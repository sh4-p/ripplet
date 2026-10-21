"""repo_map: a ranked, budgeted map of the repository for the first turn.

An agent new to a repo spends its first turns on `ls`, globbing and opening
files to find where things are. The graph already knows which files and
symbols the rest of the code depends on. This renders, most important first:

  * languages and size, entry points (routes, CLI/main, handlers),
  * per directory, the files other files depend on most, each with its
    most-used symbols as one-line signatures and how many call sites use them,
  * where the tests are.

Importance = how many call sites in OTHER files reach a symbol (calls,
instantiates, dispatches, references). Output is cut by the tool budget
from the bottom, so the least important part is what disappears.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

_SYM = ("class", "interface", "struct", "record", "enum", "trait", "type", "function", "method")
_TEST_RX = re.compile(r"(^|/)(tests?|spec|__tests__)/|(_test|_spec|\.test|\.spec|Tests?)\.[a-z]+$|"
                      r"(^|/)test_[^/]*$", re.I)
_ENTRY_NAMES = {"main", "Main", "run", "cli", "app", "create_app", "handler", "lambda_handler",
                "Program", "Startup", "index"}


def _sig(roots, rel: str, line: int, cache: dict) -> str:
    if rel not in cache:
        cache[rel] = []
        for r in roots:
            p = Path(r) / rel
            if p.is_file():
                try:
                    cache[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    pass
                break
    lines = cache[rel]
    if not (1 <= line <= len(lines)):
        return ""
    t = lines[line - 1].strip().rstrip("{").rstrip(":").strip()
    return t if len(t) <= 110 else t[:107] + "..."


def repo_map(store, cfg, focus: str = "", per_dir: int = 6, per_file: int = 4) -> str:
    cx = store.cx
    roots = [str(r) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    focus = (focus or "").replace("\\", "/").strip().lower()
    q = ",".join("?" * len(_SYM))
    nodes = {r[0]: {"type": r[1], "label": r[2], "qn": r[3], "file": (r[4] or "").replace("\\", "/"),
                    "line": r[5] or 0, "lang": r[6] or ""}
             for r in cx.execute(f"SELECT id, type, label, qualified_name, file_path, start_line, language "
                                 f"FROM nodes WHERE type IN ({q}) AND file_path<>''", _SYM)}
    if not nodes:
        return "repo_map: the index has no code symbols yet. Run `ripplet index`."
    uses = Counter()
    for tgt, efile, cnt in cx.execute(
            "SELECT target, file_path, COUNT(*) FROM edges WHERE relation IN "
            "('calls','instantiates','dispatches','references','inherits','implements') "
            "AND id NOT IN (SELECT edge_id FROM superseded_edges) GROUP BY target, file_path"):
        n = nodes.get(tgt)
        if n and (efile or "").replace("\\", "/") != n["file"]:
            uses[tgt] += cnt
    file_score = Counter()
    by_file = defaultdict(list)
    langs = Counter()
    test_files = set()
    for nid, n in nodes.items():
        if focus and focus not in n["file"].lower():
            continue
        if _TEST_RX.search(n["file"]):
            test_files.add(n["file"])
            continue
        by_file[n["file"]].append(nid)
        file_score[n["file"]] += uses[nid]
        langs[n["lang"]] += 1
    for (fp,) in cx.execute("SELECT DISTINCT file_path FROM nodes WHERE file_path<>'' AND type IN "
                            "('test','function','method','class','module')"):
        fp = (fp or "").replace("\\", "/")
        if _TEST_RX.search(fp) and (not focus or focus in fp.lower()):
            test_files.add(fp)
    all_files = set(by_file) | test_files
    out = [f"# repo_map{' (focus: ' + focus + ')' if focus else ''}",
           f"{len(all_files)} code files, {sum(len(v) for v in by_file.values())} symbols"
           + (f"; languages: {', '.join(f'{l} {c}' for l, c in langs.most_common(5) if l)}" if langs else ""),
           "Ranked by how many call sites in other files use each file/symbol (the [N]).",
           "Next: explain_symbol(<name>) for source + callers; feature_slice(<feature>) to build alike.",
           ""]
    cache: dict = {}
    # entry points: routes, mains, handlers nothing in the repo calls
    entries = []
    for r in cx.execute("SELECT e.source, e.target, e.attributes, n.file_path, n.start_line, n.label "
                        "FROM edges e JOIN nodes n ON n.id=e.target WHERE e.relation='handles_route' LIMIT 400"):
        f = (r[3] or "").replace("\\", "/")
        if not focus or focus in f.lower():
            entries.append(f"{f}:{r[4]}  {r[5]}  (route handler)")
    for nid, n in nodes.items():
        if n["label"] in _ENTRY_NAMES and not uses[nid] and not _TEST_RX.search(n["file"]) \
                and (not focus or focus in n["file"].lower()):
            entries.append(f"{n['file']}:{n['line']}  {_sig(roots, n['file'], n['line'], cache)}")
    if entries:
        out.append(f"=== Entry points ({len(entries)})")
        out.extend("  " + e for e in sorted(set(entries))[:15])
        if len(set(entries)) > 15:
            out.append(f"  ... {len(set(entries)) - 15} more")
        out.append("")
    # build, CI and deployment: how the repository is tested and run
    jobs, services, k8s = [], [], []
    for lab, fp, line, attrs in cx.execute(
            "SELECT label, file_path, start_line, attributes FROM nodes WHERE type='config'"):
        try:
            a = json.loads(attrs or "{}")
        except ValueError:
            continue
        k = a.get("kind", "")
        if k == "ci-job":
            jobs.append((lab, fp, line, a.get("runs") or []))
        elif k == "compose-service":
            services.append((lab, fp, a.get("image") or a.get("build_context") or ""))
        elif k.startswith("k8s:"):
            k8s.append((k[4:], lab, fp))
    if jobs or services or k8s:
        out.append("=== Build, CI and deployment")
        test_rx = re.compile(r"\b(test|tests|pytest|jest|vitest|mocha|rspec|phpunit|go test|cargo test|dotnet test|"
                             r"mvn|gradle|tox|nox|lint|check)\b", re.I)
        for lab, fp, line, runs in sorted(jobs, key=lambda j: (not any(test_rx.search(r) for r in j[3]), j[0]))[:8]:
            shown = [r for r in runs if test_rx.search(r)][:3] or runs[:2]
            out.append(f"  CI job {lab} ({fp}:{line})" + (": " + " && ".join(shown)[:200] if shown else ""))
        for lab, fp, src in services[:8]:
            out.append(f"  service {lab} ({fp})" + (f": {src}" if src else ""))
        for kind, lab, fp in k8s[:8]:
            out.append(f"  k8s {kind} {lab} ({fp})")
        out.append("")
    # database schema: tables the code uses most (from .sql files + sql-link)
    trows = cx.execute(
        "SELECT t.id, t.label, t.file_path, t.start_line, "
        "(SELECT COUNT(DISTINCT e.source) FROM edges e WHERE e.target=t.id AND e.method='sql-link' "
        " AND json_extract(e.attributes,'$.via') IN ('sql-literal','orm')) AS users, "
        "(SELECT COUNT(*) FROM nodes c WHERE c.type='column' AND json_extract(c.attributes,'$.table')=t.label) AS cols "
        "FROM nodes t WHERE t.type='table'").fetchall()
    if trows:
        trows = sorted(trows, key=lambda r: (-r[4], r[1]))
        out.append(f"=== Database ({len(trows)} tables; [N] = code symbols using it)")
        for tid, lab, fp, line, users, cols in trows[:12]:
            out.append(f"  {lab}  ({cols} columns, {fp}:{line})  [{users}]")
        if len(trows) > 12:
            out.append("  +" + str(len(trows) - 12) + " more: " + ", ".join(r[1] for r in trows[12:30])
                       + (" ..." if len(trows) > 30 else ""))
        out.append("  explain_symbol(<table>) / change_plan(<table or table.column>) list the code using it.")
        out.append("")
    # directories ranked by the files in them
    dirs = defaultdict(list)
    for f in by_file:
        parts = f.split("/")
        d = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else ".")
        dirs[d].append(f)
    dir_rank = sorted(dirs, key=lambda d: (-sum(file_score[f] for f in dirs[d]), d))
    for d in dir_rank:
        files = sorted(dirs[d], key=lambda f: (-file_score[f], f))
        out.append(f"=== {d}/  ({len(files)} files, used {sum(file_score[f] for f in files)}x)")
        for f in files[:per_dir]:
            syms = sorted(by_file[f], key=lambda i: (-uses[i], nodes[i]["line"]))
            shown = [i for i in syms if uses[i]][:per_file] or syms[:min(2, per_file)]
            out.append(f"  {f}  [{file_score[f]}]")
            for i in shown:
                n = nodes[i]
                sig = _sig(roots, f, n["line"], cache) or f"{n['type']} {n['label']}"
                out.append(f"      {n['line']}: {sig}" + (f"  [{uses[i]}]" if uses[i] else ""))
            rest = len(syms) - len(shown)
            if rest > 0:
                out.append(f"      (+{rest} more symbols)")
        if len(files) > per_dir:
            tail = ", ".join(Path(f).name for f in files[per_dir:per_dir + 8])
            out.append(f"  +{len(files) - per_dir} more files: {tail}{' ...' if len(files) > per_dir + 8 else ''}")
        out.append("")
    if test_files:
        tdirs = Counter("/".join(f.split("/")[:2]) if f.count("/") > 1 else f.split("/")[0] for f in test_files)
        out.append(f"=== Tests ({len(test_files)} files)")
        out.append("  " + ", ".join(f"{d}/ ({c})" for d, c in tdirs.most_common(6)))
        out.append("  check_change lists the exact tests that reach a change, with the command to run them.")
    # files the deep parser crashed on: present with text-level facts only, so
    # their callers/callees are incomplete; an agent must read them directly
    crashed = [fp for (fp,) in cx.execute(
        "SELECT file_path FROM nodes WHERE type='file' AND attributes LIKE '%\"status\": \"crashed\"%' "
        "ORDER BY file_path")]
    if crashed:
        out.append("")
        out.append(f"=== Not deep-parsed ({len(crashed)} files; the parser failed, graph facts there are partial)")
        out.append("  " + ", ".join(crashed[:12]) + (" ..." if len(crashed) > 12 else ""))
    return "\n".join(out)
