"""ripplet review: a pull request / branch reviewed against its base.

Everything `check_change` proves about a diff, plus what a reviewer asks
of a PR, as findings with a severity, a location and the evidence line:

  error    callers outside the change still using a removed/renamed symbol
           or an old signature; places still calling what the change
           replaced; code using a column/table a migration drops or
           renames; calls in new code that resolve to nothing
  warning  public symbols whose signature changed or that were removed,
           with how many places depend on them; changed functions that
           no test reaches; endpoints with frontend callers
  note     possible pattern gaps (word similarity only)

Outputs: Markdown (a PR comment), SARIF 2.1.0 (GitHub code scanning,
annotations on the diff) and JSON. `fail_on` turns findings into an exit
status for CI. Findings carry no guesses: each one names the file:line
it is about and the code there.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SEVERITY_ORDER = {"error": 3, "warning": 2, "note": 1}
_PRIVATE = re.compile(r"(^_|\.\_|^test|\.test|Test$|Tests$|^Test[A-Z_0-9]|^Benchmark[A-Z_0-9]|^Example[A-Z_0-9])")
_TESTQN = re.compile(r"(_test\.|\.test\.|\.spec\.|(^|\.)tests?\.)")


def _loc(at: str):
    f, _, ln = str(at).rpartition(":")
    try:
        return f, int(ln)
    except ValueError:
        return str(at), 1


def build_review(cfg, base: str, depth: int = 2, task: str = "") -> dict:
    from ..storage.sqlite_store import SQLiteStore
    from .check import _G, _attrs, _tests, check_change
    report = check_change(cfg, base=base, depth=depth, task=task)
    if report.get("error"):
        return {"base": base, "error": report["error"], "findings": [], "report": report}
    findings = []

    def add(sev, rule, at, message, code="", extra=None):
        f, ln = _loc(at)
        findings.append(dict({"severity": sev, "rule": rule, "file": f, "line": ln,
                              "message": message, "code": code}, **(extra or {})))

    short = lambda q: ".".join(str(q).split(".")[-2:])
    for s in report.get("stale_callers") or []:
        add("error", "stale-caller", s["at"],
            f"Still calls {short(s['symbol'])}, which this change "
            + ("removed or renamed." if s["why"] == "removed or renamed" else f"changed ({s['why']})."),
            s.get("code", ""))
    for r in report.get("replacement_gaps") or []:
        add("warning", "missed-replacement", r["at"],   # intent, not proof: another caller may need the old one
            f"Still calls {short(r['old'])}; this change replaced it with {short(r['new'])} elsewhere "
            f"(e.g. in {short(r['switched'][0])}).", r.get("code", ""))
    for x in report.get("schema_gaps") or []:
        add("error", "schema-break", x["at"], f"Uses {x['kind']} {x['schema']}, which is {x['why']}.",
            x.get("code", ""))
    for d in report.get("dangling_calls") or []:
        add("error", "unresolved-call", d["at"],
            f"Calls {d['call']}, which does not exist in this repository (closest: {d['did_you_mean']}).",
            d.get("code", ""))
    for v in report.get("rule_violations") or []:
        add(v["severity"], "rule-violation", f"{v['file']}:{v['line']}", f"[{v['rule']}] {v['message']}",
            v.get("detail", ""), {"rule_id": v["rule"]})
    if report.get("rules_error"):
        add("error", "rules-file", "ripplet.arch.yaml:1", report["rules_error"])
    for rid in report.get("rules_matched_nothing") or []:
        add("warning", "rule-matches-nothing", "ripplet.arch.yaml:1",
            f"Rule {rid} matches nothing in the graph; check its paths and names.")
    for u in report.get("unrelated_changes") or []:
        add("note", "unrelated-change", f"{u['file']}:1",
            f"Changed, but the task ({task[:80]!r}) does not lead here; intended?")
    for h in report.get("http_callers") or []:
        add("warning", "endpoint-callers", h["at"],
            f"{h['http']} is served by {short(h['handler'])}, which this change touches; this caller depends on it.")
    for p in report.get("pattern_gaps") or []:
        add("note", "possible-pattern-gap", p["at"],
            f"{short(p['candidate'])} shares {', '.join(p['shares'])} with the new helper "
            f"{p['helper'].rsplit('.', 1)[-1]} but does not call it (word similarity only; verify).")

    st = SQLiteStore(cfg.storage_path)
    try:
        g = _G(st.cx)
        # public API: removed or re-signatured symbols other code depends on
        removed = set(report.get("removed") or [])
        for sc in report.get("signature_changes") or []:
            q = sc["symbol"]
            if _PRIVATE.search(q.rsplit(".", 1)[-1]):
                continue
            # the definition, not an import placeholder with the same name
            ids = [i for i, n in g.nodes.items() if n["qn"] == q and not i.startswith("ext:")
                   and n["type"] != "external_symbol"]
            users = {s for i in ids for s, _t, rel, *_r in g.inc.get(i, ()) if rel in ("calls", "references", "instantiates")}
            n = g.nodes[ids[0]] if ids else None
            if n is not None:
                add("warning", "api-signature", f"{n['file']}:{n['line']}",
                    f"Public {n['type']} {short(q)} changed parameters {sc['before']} -> {sc['after']}; "
                    f"{len(users)} place(s) depend on it.")
        for q in sorted(removed):
            if not _PRIVATE.search(q.rsplit(".", 1)[-1]) and not _TESTQN.search(q):
                # no location in the new tree: reported without one (SARIF skips it)
                add("warning", "api-removed", "(removed):0",
                    f"Public symbol {short(q)} was removed or renamed; callers outside the change are listed as errors.")
        # names that tests call without the graph being able to prove the receiver (`resp.json()`): a changed
        # method of that name is probably tested, so it is not reported as untested
        test_called = set()
        for nid_, n_ in g.nodes.items():
            if g.is_test(nid_):
                for _s, _t, rel_, _f, _ln, at_ in g.out.get(nid_, ()):
                    if rel_ == 'calls_unresolved':
                        a_ = _attrs(at_)
                        test_called.add(a_.get('member') or str(a_.get('unresolved_call') or '').rsplit('.', 1)[-1])
        test_called.discard('')
        # changed functions/methods that no test reaches
        for nid in report.get("changed_ids") or []:
            n = g.nodes.get(nid)
            if not n or n["type"] not in ("function", "method") or g.is_test(nid):
                continue
            # private helpers count too: a bug there is still a bug
            if n['label'] in test_called:
                continue
            if not _tests(g, [nid], depth=4):
                add("warning", "untested-change", f"{n['file']}:{n['line']}",
                    f"{n['type'].capitalize()} {short(n['qn'])} changed and no test reaches it.")
    finally:
        st.close()
    findings.sort(key=lambda f: (-SEVERITY_ORDER[f["severity"]], f["file"], f["line"]))
    tests = report.get("tests") or {}
    return {"base": base, "summary": report.get("summary", ""), "files": report.get("files", []),
            "findings": findings, "tests": {"total": tests.get("total", 0), "commands": tests.get("commands", [])},
            "counts": {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITY_ORDER}}


def to_markdown(r: dict, max_per_rule: int = 15) -> str:
    """Errors first and open; warnings and notes folded away, so the comment stays short on a clean PR."""
    if r.get("error"):
        return f"**Ripplet review** could not run: {r['error']}"
    c = r["counts"]
    base = str(r["base"])[:10]
    n_files = len(r["files"])
    if c["error"]:
        verdict = f":x: **{c['error']} problem(s) the change left behind**"
    elif c["warning"] or c["note"]:
        verdict = ":white_check_mark: **Nothing left behind.** Some points to look at below."
    else:
        verdict = (":white_check_mark: **Nothing left behind.** Every caller, replacement and schema use "
                   "is consistent with the change.")
    head = (f"### Ripplet review\n{verdict}\n\n<sub>{n_files} file(s) changed against `{base}` · "
            f"{c['error']} error(s), {c['warning']} warning(s), {c['note']} note(s)</sub>\n")
    titles = {"stale-caller": "Callers left on the old symbol or signature",
              "missed-replacement": "Places still calling what this change replaced",
              "schema-break": "Code using a dropped or renamed column/table",
              "unresolved-call": "Calls that resolve to nothing",
              "api-signature": "Public API signature changes", "api-removed": "Public API removals",
              "untested-change": "Changed code no test reaches",
              "endpoint-callers": "Callers of changed endpoints",
              "rule-violation": "Architecture rule violations",
              "rules-file": "Architecture rules file",
              "rule-matches-nothing": "Architecture rules that match nothing",
              "unrelated-change": "Changes the task does not lead to",
              "possible-pattern-gap": "Possible pattern gaps (unverified)"}
    icon = {"error": ":x:", "warning": ":warning:", "note": ":information_source:"}
    out = [head]

    def section(fs, cap):
        lines = []
        for f in fs[:cap]:
            loc = f"`{f['file']}:{f['line']}`" if f["line"] else ""
            lines.append(f"- {loc} {f['message']}" + (f"\n  `{f['code'][:160]}`" if f.get("code") else ""))
        if len(fs) > cap:
            lines.append(f"- ... {len(fs) - cap} more")
        return lines

    rules = list(titles) + sorted({f["rule"] for f in r["findings"]} - set(titles))
    for sev in ("error", "warning", "note"):
        for rule in rules:
            fs = [f for f in r["findings"] if f["rule"] == rule and f["severity"] == sev]
            if not fs:
                continue
            title = f"{icon[sev]} {titles.get(rule, rule)} ({len(fs)})"
            if sev == "error":
                out.append(f"\n**{title}**\n")
                out.extend(section(fs, max_per_rule))
            else:
                out.append(f"\n<details><summary>{title}</summary>\n")
                out.extend(section(fs, 8 if sev == "warning" else 5))
                out.append("\n</details>")
    if r["tests"]["commands"]:
        out.append(f"\n<details><summary>Tests that reach this change ({r['tests']['total']})</summary>\n")
        out.extend(f"```\n{cmd}\n```" for cmd in r["tests"]["commands"][:3])
        out.append("\n</details>")
    out.append("\n<sub>Evidence-based review by [Ripplet](https://github.com/sh4-p/ripplet): "
               "no LLM, every finding has a file and line.</sub>")
    return "\n".join(out) + "\n"


def to_sarif(r: dict) -> dict:
    rules = sorted({f["rule"] for f in r.get("findings", [])})
    level = {"error": "error", "warning": "warning", "note": "note"}
    results = []
    for f in r.get("findings", []):
        if not f["line"]:
            continue
        results.append({"ruleId": f["rule"], "level": level[f["severity"]],
                        "message": {"text": f["message"] + (f"  Code: {f['code']}" if f.get("code") else "")},
                        "locations": [{"physicalLocation": {
                            "artifactLocation": {"uri": f["file"].replace("\\", "/")},
                            "region": {"startLine": max(1, int(f["line"]))}}}]})
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "Ripplet", "informationUri": "https://pypi.org/project/ripplet/",
                                          "rules": [{"id": x, "shortDescription": {"text": x.replace("-", " ")}}
                                                    for x in rules]}},
                      "results": results}]}


def exit_code(r: dict, fail_on: str) -> int:
    if r.get("error"):
        return 2
    if fail_on == "never":
        return 0
    limit = SEVERITY_ORDER.get(fail_on, 3)
    return 1 if any(SEVERITY_ORDER[f["severity"]] >= limit for f in r["findings"]) else 0


def render(r: dict, fmt: str) -> str:
    if fmt == "sarif":
        return json.dumps(to_sarif(r), indent=2)
    if fmt == "json":
        return json.dumps(r, indent=2, default=str)
    return to_markdown(r)
