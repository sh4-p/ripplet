"""check_change: one call after an edit that answers "what did I miss, what
did I break, which tests prove it".

Pipeline (read-only for the workspace; it re-indexes the changed files):
  1. changed files from git (tracked diff + untracked files), with hunks;
  2. snapshot of the symbols in those files *before* re-indexing (their
     signatures and who called them), then an incremental re-index;
  3. changed / added / removed symbols, signature (arity) changes, and the
     callers outside the change set that still use the old shape;
  4. calls inside the changed code that no longer resolve to anything;
  5. pattern completeness: a helper introduced by this change is called
     from some places; other places touching the same data (shared
     identifiers, weighted by rarity) that do not call it are flagged;
  6. dependents of the change (including frontend callers over HTTP) and
     the tests that reach it, with ready-to-run commands per language.

Everything is evidence from the graph and the files; nothing is guessed
without a stated reason, and each finding carries file:line.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict, deque
from pathlib import Path

from ..testplan import changes as ch

SYMBOL_TYPES = ("function", "method", "class", "interface", "struct", "type", "trait",
                "enum", "test", "object", "record")
CALLABLE = ("function", "method", "test")
DEP_RELS = ("calls", "instantiates", "calls_http", "inherits", "implements", "references", "uses", "dispatches")
_TEST_PATH = re.compile(r"(^|/)(tests?|specs?|e2e|__tests__|test-d)/|[._-](test|spec)\.|Tests?\.\w+$|_test\.go$")
_TEST_FILE = re.compile(r"(Tests?\.(cs|java|kt|php)$|[._-](test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]*\.py$|_test\.(py|go)$)")
_COMMENT = re.compile(r"^\s*(//|#|\*|/\*|--)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_STOP = {"this", "self", "return", "public", "private", "protected", "static", "async", "await", "const",
         "var", "let", "function", "class", "string", "null", "true", "false", "void", "None", "True",
         "False", "new", "import", "from", "using", "namespace", "readonly", "override", "virtual",
         "default", "value", "result", "Task", "ValueTask", "CancellationToken", "cancellationToken",
         "List", "Array", "Object", "Promise", "Observable", "string", "number", "boolean", "else",
         "throw", "catch", "finally", "foreach", "where", "select", "Select", "Where", "ToList",
         "FirstOrDefault", "Count", "Length", "length", "message", "context", "request", "response",
         "options", "params", "args", "data", "item", "items", "error", "errors", "test", "Assert"}


# ------------------------------------------------------------------ inputs
def _changed_files(root: str, base: str | None) -> list:
    if not ch.is_repo(root):
        return []
    files = ch.diff_files(root, base, "", separator="...") if base else ch.working_tree_files(root)
    # untracked files are invisible to `git diff HEAD`; agents create them all the time
    _, out = ch._git(root, "ls-files", "--others", "--exclude-standard")
    known = {f.rel for f in files}
    added = 0
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel or rel in known or _VENDOR.search(rel) or Path(rel).suffix.lower() not in _CODE_EXT:
            continue
        # a new file is changed from its first line to its last
        files.append(ch.ChangedFile(rel, "added", new_ranges=[(1, 10 ** 9)]))
        added += 1
        if added >= 500:   # an un-ignored dependency folder must not stall the report
            break
    return files


_VENDOR = re.compile(r"(^|/)(node_modules|vendor|bower_components|dist|build|out|target|bin|obj|\.gradle|Pods|"
                     r"\.venv|venv|__pycache__|\.next|\.nuxt|coverage|DerivedData)/")
_CODE_EXT = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".java", ".kt", ".kts", ".go", ".rs", ".cs", ".php",
             ".rb", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".lua", ".dart", ".gd", ".scala", ".vue", ".svelte",
             ".html", ".sql"}


def _added_lines(root: str, base: str | None) -> dict:
    """rel -> list of added line texts (tracked diff) ; untracked files: all lines."""
    spec = [base + "..."] if base else ["HEAD"]
    try:
        _, out = ch._git(root, "diff", *spec, "-U0", "--no-color")
    except ValueError:
        return {}
    res, cur = defaultdict(list), None
    for ln in out.splitlines():
        if ln.startswith("+++ "):
            cur = ln[6:] if ln.startswith("+++ b/") else None
        elif cur and ln.startswith("+") and not ln.startswith("+++"):
            res[cur].append(ln[1:])
    return res


def _added_line_numbers(root: str, base: str | None) -> dict:
    """rel -> new-side line numbers the diff adds or changes."""
    spec = [base + "..."] if base else ["HEAD"]
    try:
        _, out = ch._git(root, "diff", *spec, "-U0", "--no-color")
    except ValueError:
        return {}
    res, cur = defaultdict(set), None
    for ln in out.splitlines():
        if ln.startswith("+++ "):
            cur = ln[6:] if ln.startswith("+++ b/") else None
        elif cur and ln.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", ln)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                res[cur].update(range(start, start + count))
    return res


# --------------------------------------------------------------- snapshots
def _attrs(raw):
    try:
        return json.loads(raw or "{}") or {}
    except ValueError:
        return {}


def _symbols_in(cx, files: set) -> dict:
    """qn -> {id, type, file, line, end, pmin, pmax} for symbols in `files`."""
    out = {}
    if not files:
        return out
    fl = list(files)
    for i in range(0, len(fl), 400):
        chunk = fl[i:i + 400]
        q = ",".join("?" * len(chunk))
        for r in cx.execute(f"SELECT id, type, qualified_name, label, file_path, start_line, end_line, attributes "
                            f"FROM nodes WHERE file_path IN ({q}) AND type IN ({','.join('?' * len(SYMBOL_TYPES))})",
                            chunk + list(SYMBOL_TYPES)):
            a = _attrs(r[7])
            out.setdefault(r[2] or r[3], {"id": r[0], "type": r[1], "label": r[3], "file": r[4], "line": r[5] or 0,
                                          "end": r[6] or r[5] or 0, "pmin": a.get("params_min"),
                                          "pmax": a.get("params_max")})
    return out


def _callers_of(cx, ids: list) -> dict:
    """target id -> [(caller id, caller qn, file, line, relation)] for dependency edges."""
    out = defaultdict(list)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        q = ",".join("?" * len(chunk))
        for t, s, sq, f, ln, rel, attrs in cx.execute(
                f"SELECT e.target, e.source, n.qualified_name, e.file_path, e.start_line, e.relation, "
                f"e.attributes FROM edges e "
                f"JOIN nodes n ON n.id=e.source WHERE e.target IN ({q}) AND e.relation IN "
                f"({','.join('?' * len(DEP_RELS))}) "
                # a name-only guess (hypothetical) never becomes a DEFINITE stale caller
                f"AND COALESCE(e.confidence_class, '') <> 'hypothetical'", chunk + list(DEP_RELS)):
            for line in occurrence_lines(ln or 0, attrs):
                out[t].append((s, sq, f or "", line, rel))
    return out


def occurrence_lines(line: int, attrs) -> list:
    """Every line of one caller->callee edge. One edge stands for all calls
    from the same caller; the extra call sites are in `occurrences`, and each
    is a separate place a change can miss."""
    out = [line] if line else []
    a = _attrs(attrs) if not isinstance(attrs, dict) else attrs
    for o in a.get("occurrences") or ():
        ln = o.get("line") if isinstance(o, dict) else None
        if isinstance(ln, int) and ln > 0 and ln not in out:
            out.append(ln)
    return out


# ------------------------------------------------------------------- graph
class _G:
    def __init__(self, cx):
        self.nodes = {}
        for r in cx.execute("SELECT id, type, label, qualified_name, file_path, start_line, end_line, language "
                            "FROM nodes"):
            self.nodes[r[0]] = {"id": r[0], "type": r[1], "label": r[2] or "", "qn": r[3] or r[2] or r[0],
                                "file": r[4] or "", "line": r[5] or 0, "end": r[6] or r[5] or 0, "lang": r[7] or ""}
        self.inc, self.out = defaultdict(list), defaultdict(list)
        sup = set()
        try:
            sup = {r[0] for r in cx.execute("SELECT edge_id FROM superseded_edges")}
        except Exception:
            pass
        for eid, s, t, rel, f, ln, attrs in cx.execute(
                "SELECT id, source, target, relation, file_path, start_line, attributes FROM edges"):
            if eid in sup:
                continue
            e = (s, t, rel, f or "", ln or 0, attrs or "")
            self.out[s].append(e)
            self.inc[t].append(e)

    def is_test(self, nid) -> bool:
        n = self.nodes.get(nid)
        return bool(n) and (n["type"] == "test" or bool(_TEST_PATH.search(n["file"])))

    def owner_symbol(self, nid):
        """A member's enclosing symbol chain stops at the first SYMBOL_TYPES node."""
        return nid


def _src(roots, rel, a, b) -> list:
    for r in roots:
        p = Path(r) / rel
        if p.is_file():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return []
            return lines[max(0, a - 1):max(a, b)]
    return []


# -------------------------------------------------------------------- main
def check_change(cfg, base: str | None = None, depth: int = 2, reindex: bool = True,
                 max_items: int = 25, task: str = "") -> dict:
    from ..service import index_workspace
    from ..storage.sqlite_store import SQLiteStore
    roots = list(cfg.workspace_roots or [])
    changed: list = []
    added_text: dict = {}
    added_lines_at: dict = {}      # rel -> line numbers this change wrote (new side)
    for root in roots:
        try:
            for f in _changed_files(root, base):
                changed.append((root, f))
            for rel, lines in _added_lines(root, base).items():
                added_text.setdefault(rel, []).extend(lines)
            for rel, nums in _added_line_numbers(root, base).items():
                added_lines_at.setdefault(rel, set()).update(nums)
        except ValueError as e:
            return {"error": str(e)}
    rels = {f.rel for _, f in changed}
    report: dict = {"base": base or "working tree", "files": [{"path": f.rel, "status": f.status} for _, f in changed]}
    if not changed:
        report["summary"] = "No changes against " + report["base"] + "."
        return report

    st = SQLiteStore(cfg.storage_path)
    before = _symbols_in(st.cx, rels)
    before_callers = _callers_of(st.cx, [v["id"] for v in before.values()])
    before_callees = _callees_of(st.cx, [v["id"] for v in before.values()])
    st.close()
    if reindex:
        index_workspace(cfg)
    st = SQLiteStore(cfg.storage_path)
    cx = st.cx
    after = _symbols_in(cx, rels)
    g = _G(cx)

    # ---- changed symbols (by hunk), added, removed, signature changes
    changed_ids, added_q, removed_q, sig = set(), [], [], []
    by_file = defaultdict(list)
    for q, s in after.items():
        by_file[s["file"]].append((q, s))
    for _root, f in changed:
        for q, s in by_file.get(f.rel, []):
            if f.status == "added" or not f.new_ranges or any(a <= s["end"] and s["line"] <= b for a, b in f.new_ranges):
                changed_ids.add(s["id"])
    for q, s in after.items():
        if q not in before:
            added_q.append(q)
            changed_ids.add(s["id"])
    for q, s in before.items():
        if q not in after:
            removed_q.append(q)
        else:
            a = after[q]
            if (s["pmin"], s["pmax"]) != (a["pmin"], a["pmax"]) and None not in (s["pmin"], a["pmin"]):
                sig.append((q, s, a))

    # ---- callers left behind: removed symbols / changed arity, caller outside the change set
    stale, stale_seen = [], set()
    for q in removed_q:
        old_name = before[q].get("label") or q.rsplit(".", 1)[-1]
        for cid, cq, f, ln, rel in before_callers.get(before[q]["id"], []):
            if f in rels:
                continue
            # the untouched caller must still name the old symbol on that line
            # (or a few lines lower, for a call chained over several lines)
            site = site_line(roots, f, ln, old_name)
            if site and (f, site[0], q) not in stale_seen:
                stale_seen.add((f, site[0], q))
                stale.append({"symbol": q, "why": "removed or renamed", "caller": cq,
                              "at": f"{f}:{site[0]}", "code": site[1]})
    # callers inside the changed files: a line the change did not touch that
    # still calls the old name (`fetch` next to an updated `create`)
    if removed_q:
        old_names = {before[q].get("label") or q.rsplit(".", 1)[-1]: q for q in removed_q}
        for nid, n in g.nodes.items():
            if n["file"] not in rels:
                continue
            for s_, t_, rel_, f_, ln_, attrs_ in g.out.get(nid, ()):
                if rel_ != "calls_unresolved" or f_ not in rels:
                    continue
                a_ = _attrs(attrs_)
                nm = a_.get("member") or a_.get("base") or str(a_.get("unresolved_call") or "").rsplit(".", 1)[-1]
                q = old_names.get(nm)
                if not q:
                    continue
                for line in occurrence_lines(ln_ or 0, attrs_):
                    site = site_line(roots, f_, line, nm, window=0)
                    if not site or site[0] in added_lines_at.get(f_, set()) or (f_, site[0], q) in stale_seen:
                        continue
                    stale_seen.add((f_, site[0], q))
                    stale.append({"symbol": q, "why": "removed or renamed", "caller": n["qn"],
                                  "at": f"{f_}:{site[0]}", "code": site[1]})
    arity_at = {}
    for q, b, a in sig:
        for s_, t_, rel_, f_, ln_, attrs_ in g.inc.get(a["id"], ()):
            try:
                ar = json.loads(attrs_ or "{}").get("arity")
            except ValueError:
                ar = None
            if ar is not None:
                arity_at[(f_, ln_)] = ar
    for q, b, a in sig:
        breaking = (a["pmin"] or 0) > (b["pmin"] or 0) or (
            a["pmax"] is not None and (b["pmax"] is None or a["pmax"] < b["pmax"]))
        if not breaking:
            continue          # an added optional parameter breaks nobody
        for cid, cq, f, ln, rel in before_callers.get(b["id"], []):
            if f in rels:
                continue
            ar = arity_at.get((f, ln))
            if ar is not None and (a["pmin"] or 0) <= ar <= (a["pmax"] if a["pmax"] is not None else ar):
                continue      # this call already fits the new signature
            stale.append({"symbol": q, "why": f"parameters {b['pmin']}-{b['pmax']} -> {a['pmin']}-{a['pmax']}",
                          "caller": cq, "at": f"{f}:{ln}", "code": code_line(roots, f, ln)})

    # ---- calls in the changed code that resolve to nothing in the repo
    labels = {n["label"] for n in g.nodes.values() if n["type"] in SYMBOL_TYPES}
    dangling = []
    for nid in changed_ids:
        for s, t, rel, f, ln, attrs in g.out.get(nid, ()):
            if rel != "calls_unresolved" or f not in rels:
                continue
            a = _attrs(attrs)
            member = a.get("member") or str(t).rsplit(":", 1)[-1].rsplit(".", 1)[-1]
            text = " ".join(added_text.get(f, []))
            # evidence that the call means *this repo's* code: a bare call, a
            # call on this/self, or on a receiver typed as a repo type.
            # `fmt.Errorf`, `os.path.join`, `JSON.parse` are other libraries.
            spelled = a.get("unresolved_call") or ""
            base = a.get("base") or (spelled.rsplit(".", 1)[0] if "." in spelled else "")
            full = str(a.get("full") or "")
            if not base and "::" in full:
                # Rust/C++ path call `ptr::write_bytes(..)`: a module/type path,
                # repo code only when it starts at crate/self/super
                root_ = full.split("::")[0]
                if root_ not in ("crate", "self", "super", "Self"):
                    continue
            rtype = a.get("receiver_type") or ""
            if a.get("callee_kind") == "complex" and not rtype:
                continue      # `x.AsSpan().Slice()`, `.Errors.Count()`: a chain on an expression
            if base and base not in ("this", "self", "base", "super") and not (
                    rtype and any(n["label"] == rtype.split("<")[0].split(".")[-1] and n["type"] in SYMBOL_TYPES
                                  for n in g.nodes.values())):
                continue
            # only names the change itself wrote, and that look like repo code
            # (a near-miss of a repo symbol name): typos and removed helpers
            if a.get("bare"):
                continue      # Ruby bare name: maybe a local the extractor cannot see
            if member in _BUILTIN_CALLS or (f.endswith(".py") and member in _PY_BUILTINS):
                continue      # `super().x()`, `print(...)`: the language's own names
            if member and len(member) >= 5 and member in text and member not in labels \
                    and not _declared_in(roots, f, member):
                near = _near(member, labels)
                if near:
                    dangling.append({"call": member, "at": f"{f}:{ln}", "did_you_mean": near,
                                     "code": code_line(roots, f, ln)})

    # ---- pattern completeness
    gaps = _pattern_gaps(g, roots, changed_ids, set(added_q) | {q for q in after if after[q]["id"] in changed_ids},
                         after, max_items)
    replaced = _replacement_gaps(g, roots, before, before_callees, after, set(added_q), rels, DEFINITE_CAP)

    # ---- dependents (impact) and tests
    dist = {c: 0 for c in changed_ids}
    dq = deque((c, 0) for c in changed_ids)
    http_callers = []
    while dq:
        x, d = dq.popleft()
        if d >= depth:
            continue
        for s, t, rel, f, ln, attrs in g.inc.get(x, ()):
            if rel not in DEP_RELS or s in dist or s not in g.nodes:
                continue
            dist[s] = d + 1
            dq.append((s, d + 1))
            if rel == "calls_http" and not g.is_test(s):
                a = _attrs(attrs)
                http_callers.append({"caller": g.nodes[s]["qn"], "at": f"{f}:{ln}",
                                     "http": f"{a.get('http_method', '')} {a.get('url', '')}".strip(),
                                     "handler": g.nodes[x]["qn"]})
    dependents = [g.nodes[n] for n, d in sorted(dist.items(), key=lambda kv: kv[1])
                  if d > 0 and not g.is_test(n) and g.nodes[n]["file"] not in rels]
    tests = _tests(g, changed_ids, depth=4)
    for r in rels:  # changed test files are tests to run too
        for q, s in by_file.get(r, []):
            if g.is_test(s["id"]):
                tests.add(s["id"])

    schema = _schema_gaps(cx, roots, rels, added_text)
    report.update({
        "schema_gaps": schema[:DEFINITE_CAP],
        "changed_symbols": [_brief(g, i) for i in sorted(changed_ids, key=lambda i: (g.nodes[i]["file"], g.nodes[i]["line"]))][:max_items * 2],
        "changed_ids": sorted(changed_ids),
        "added": added_q[:max_items], "removed": removed_q[:max_items],
        "signature_changes": [{"symbol": q, "before": f"{b['pmin']}-{b['pmax']}", "after": f"{a['pmin']}-{a['pmax']}"} for q, b, a in sig],
        "stale_callers": stale[:DEFINITE_CAP], "dangling_calls": dangling[:DEFINITE_CAP],
        "pattern_gaps": gaps, "replacement_gaps": replaced, "http_callers": http_callers[:max_items],
        "dependents": [{"symbol": n["qn"], "at": f"{n['file']}:{n['line']}"} for n in dependents[:max_items]],
        "dependents_total": len(dependents),
        "tests": _test_commands(g, tests, roots),
    })
    # architecture rules (ripplet.arch.yaml) broken in the changed files
    from .rules import check_rules
    rr = check_rules(cfg, files=rels)
    report["rule_violations"] = rr.get("violations", [])[:DEFINITE_CAP]
    if rr.get("error"):
        report["rules_error"] = rr["error"]
    if rr.get("matched_nothing"):
        report["rules_matched_nothing"] = rr["matched_nothing"]
    # access gaps elsewhere (built-in check): not caused by this change, but an
    # agent auditing access must not finish without seeing them
    try:
        elsewhere = [v for v in check_rules(cfg).get("builtin", []) if v["file"] not in rels]
    except Exception:  # noqa: BLE001
        elsewhere = []
    if elsewhere:
        report["access_gaps_elsewhere"] = elsewhere[:10]
    if task:
        report["task"] = task
        report["unrelated_changes"] = _unrelated_changes(st, cfg, g, task, changed)
    report["summary"] = _summary(report)
    st.close()
    return report


_NON_CODE = re.compile(r"(^|/)(docs?|\.github)/|\.(md|rst|txt|lock|ya?ml|json|toml|cfg|ini)$|(^|/)(CHANGELOG|README)", re.I)


def _unrelated_changes(st, cfg, g, task: str, changed) -> list:
    """Changed code files the task does not lead to: not among the files
    task_context names for it, nor one dependency step from them."""
    from ..retrieval.task_context import task_files
    want = set(task_files(st, cfg, task, limit=10))
    if not want:
        return []  # the task matched no code: nothing to compare against
    near = set(want)
    for nid, n in g.nodes.items():
        if n["file"] not in want:
            continue
        for s, *_r in g.inc.get(nid, ()):
            if s in g.nodes:
                near.add(g.nodes[s]["file"])
        for t, *_r in g.out.get(nid, ()):
            if t in g.nodes:
                near.add(g.nodes[t]["file"])
    out = []
    for _root, f in changed:
        if f.rel in near or _NON_CODE.search(f.rel):
            continue
        if any(g.is_test(i) for i, n in g.nodes.items() if n["file"] == f.rel):
            continue
        out.append({"file": f.rel, "status": f.status,
                    "why": "not among the files the task leads to, nor one dependency step from them"})
    return out


def _brief(g, nid):
    n = g.nodes[nid]
    return {"symbol": n["qn"], "type": n["type"], "at": f"{n['file']}:{n['line']}", "id": nid}


def _callees_of(cx, ids: list) -> dict:
    """caller id -> {target qualified name: first call line} (resolved calls/instantiations)."""
    out = defaultdict(dict)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        q = ",".join("?" * len(chunk))
        for s, tq, ln in cx.execute(
                f"SELECT e.source, n.qualified_name, e.start_line FROM edges e JOIN nodes n ON n.id=e.target "
                f"WHERE e.source IN ({q}) AND e.relation IN ('calls','instantiates')", chunk):
            out[s].setdefault(tq, ln or 0)
    return out


def code_line(roots, rel: str, line: int) -> str:
    """The source text at rel:line (stripped, shortened) — evidence a reader can check."""
    for r in roots:
        p = Path(r) / rel
        if p.is_file():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return ""
            if 1 <= line <= len(lines):
                t = lines[line - 1].strip()
                return t if len(t) <= 140 else t[:137] + "..."
    return ""


# Definite findings (proven by graph + diff) are all kept: a change that
# missed 70 call sites must hear about 70, not the first 25.
DEFINITE_CAP = 1000


def rest_by_file(items: list, key: str = "at", limit_files: int = 40) -> list:
    """`path: 12, 45, 88` lines for findings not shown in full."""
    from collections import OrderedDict
    by = OrderedDict()
    for it in items:
        f, _, ln = str(it.get(key, "")).rpartition(":")
        by.setdefault(f, []).append(ln)
    out = [f"  {f}: {', '.join(lns)}" for f, lns in list(by.items())[:limit_files]]
    if len(by) > limit_files:
        out.append(f"  ... {len(by) - limit_files} more files")
    return out


def site_line(roots, rel: str, line: int, name: str, window: int = 6):
    """(line, code) of the call to `name` that the edge at rel:line records.

    An edge sits on the statement's first line; a call chained over several
    lines (`article = await context` / `.Articles.GetAllData()`) names the
    callee a few lines lower. Returns None when the name is not there at all:
    without the name on the line there is no evidence to show."""
    rx = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    for r in roots:
        p = Path(r) / rel
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for ln in range(max(1, line), min(len(lines), line + window) + 1):
            if rx.search(lines[ln - 1]):
                t = lines[ln - 1].strip()
                return ln, (t if len(t) <= 140 else t[:137] + "...")
        return None
    return None


# Names a language provides itself: a call to them is never a dangling repo call.
_BUILTIN_CALLS = {"super", "this", "base", "self", "print", "require", "typeof", "sizeof",
                  "nameof", "assert", "isinstance", "getattr", "setattr", "hasattr"}
try:
    import builtins as _bi
    _PY_BUILTINS = set(dir(_bi))
except Exception:       # pragma: no cover
    _PY_BUILTINS = set()


def _replacement_gaps(g, roots, before, before_callees, after, added_q, rels, max_items) -> list:
    """The change replaced calls to S with calls to H in some callers (S still
    exists). Callers that still call S are the places the replacement missed.

    Evidence-only: a caller counts as switched when its before-callees had S
    and its after-callees have H but not S. H must be introduced by this
    change or call S itself (a wrapper), so unrelated edits never pair up.
    """
    # real symbols only: an import also leaves an `ext:` placeholder with the same name
    q2id = {n["qn"]: nid for nid, n in g.nodes.items() if n["type"] in SYMBOL_TYPES and not nid.startswith("ext:")}
    after_by_q = {q: s for q, s in after.items()}
    switched = defaultdict(set)     # (S qn, H qn) -> callers that switched
    for q, b in before.items():
        a = after_by_q.get(q)
        if a is None:
            continue
        b_set = set(before_callees.get(b["id"], {}))
        a_set = {g.nodes[t]["qn"] for _s, t, rel, *_ in g.out.get(a["id"], ())
                 if rel in ("calls", "instantiates") and t in g.nodes}
        removed = {x for x in b_set - a_set if x in q2id}
        added = a_set - b_set
        for s_q in removed:
            for h_q in added:
                h = q2id.get(h_q)
                if h is None or h_q == s_q:
                    continue
                wraps = any(g.nodes.get(t, {}).get("qn") == s_q for _s, t, rel, *_ in g.out.get(h, ())
                            if rel in ("calls", "instantiates"))
                if h_q in added_q or wraps:
                    switched[(s_q, h_q)].add(q)
    out, seen = [], set()
    for (s_q, h_q), callers in switched.items():
        s_id = q2id[s_q]
        h_id = q2id[h_q]
        for src, _t, rel, f, ln0, _a in g.inc.get(s_id, ()):
            n = g.nodes.get(src)
            if rel not in ("calls", "instantiates") or n is None or src == h_id or n["qn"] in callers \
                    or g.is_test(src):
                continue
            # the helper's own internals and its siblings are not misses
            if n["file"] == g.nodes[h_id]["file"]:
                continue
            for ln in occurrence_lines(ln0, _a):
                site = site_line(roots, f, ln, s_q.rsplit(".", 1)[-1])
                if site is None or (f, site[0]) in seen:
                    continue      # the old name is not written there: no evidence
                seen.add((f, site[0]))
                out.append({"caller": n["qn"], "at": f"{f}:{site[0]}", "code": site[1],
                            "old": s_q, "new": h_q, "switched": sorted(callers)[:3]})
    return out[:max_items]


def _declared_in(roots, rel: str, name: str) -> bool:
    """`name` is used somewhere in the file other than as a call (`article =
    input()`, `const article`, a parameter): a local value, not a missing symbol."""
    for r in roots:
        p = Path(r) / rel
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return False
            return re.search(r"\b" + re.escape(name) + r"\b(?!\s*\()", text) is not None
    return False


def _near(name: str, labels: set) -> str:
    best, bd = "", 3
    low = name.lower()
    for lab in labels:
        if abs(len(lab) - len(name)) > 2 or lab[:1].lower() != low[:1]:
            continue
        if lab.lower() == low:
            continue     # `article()` vs `Article`: a local (signal) named like a type, not a typo
        d = _lev(low, lab.lower(), bd)
        if d < bd:
            best, bd = lab, d
    return best


def _lev(a: str, b: str, cap: int) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) >= cap:
            return cap
        prev = cur
    return prev[-1]


def _tests(g, seeds, depth=4) -> set:
    found, seen, dq = set(), set(seeds), deque((s, 0) for s in seeds)
    while dq:
        x, d = dq.popleft()
        if x not in seeds and g.is_test(x):
            found.add(x)
            continue
        if d >= depth:
            continue
        for s, t, rel, f, ln, _a in g.inc.get(x, ()):
            if rel in DEP_RELS and s not in seen:
                seen.add(s)
                dq.append((s, d + 1))
    return found


def _test_commands(g, tests: set, roots) -> dict:
    by_file = defaultdict(list)
    for t in tests:
        n = g.nodes.get(t)
        if n:
            by_file[n["file"]].append(n)
    cmds = []
    cs_classes, vitest, pytest_ids, go_pk, php, jvm = set(), set(), [], set(), set(), set()
    for f, ns in sorted(by_file.items()):
        if not _TEST_FILE.search(f):
            continue  # helpers reached on the way (ArticleHelpers.cs, e2e/helpers/api.ts)
        ext = Path(f).suffix
        if ext == ".cs":
            cs_classes.add(Path(f).stem)
        elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
            vitest.add(f)
        elif ext == ".py":
            pytest_ids.append(f)
        elif ext == ".go":
            go_pk.add("./" + str(Path(f).parent).replace("\\", "/"))
        elif ext == ".php":
            php.add(f)
        elif ext in (".kt", ".java"):
            jvm.add(Path(f).stem)
    if cs_classes:
        cmds.append('dotnet test --filter "' + "|".join(f"FullyQualifiedName~{c}" for c in sorted(cs_classes)) + '"')
    if vitest:
        cmds.append("npx vitest run " + " ".join(sorted(_rel_to_pkg(roots, f) for f in vitest)))
    if pytest_ids:
        cmds.append("pytest " + " ".join(sorted(set(pytest_ids))))
    if go_pk:
        cmds.append("go test " + " ".join(sorted(go_pk)))
    if php:
        cmds.append("vendor/bin/phpunit " + " ".join(sorted(php)))
    if jvm:
        cmds.append("./gradlew test " + " ".join(f"--tests '*{c}'" for c in sorted(jvm)))
    return {"files": [{"file": f, "tests": [n["label"] for n in ns][:8]} for f, ns in sorted(by_file.items())][:40],
            "total": len(tests), "commands": cmds}


def _rel_to_pkg(roots, rel: str) -> str:
    """Path relative to the nearest package.json (vitest runs from there)."""
    for r in roots:
        p = Path(r) / rel
        for parent in p.parents:
            if (parent / "package.json").exists():
                try:
                    return str(p.relative_to(parent)).replace("\\", "/")
                except ValueError:
                    break
            if parent == Path(r):
                break
    return rel


def _pattern_gaps(g, roots, changed_ids, changed_q, after, max_items) -> list:
    """Helpers this change introduced, and the places that touch the same
    data as the helper's callers but do not call it."""
    added_ids = {after[q]["id"] for q in changed_q if q in after}
    helpers = defaultdict(set)  # helper id -> changed callers
    for c in changed_ids:
        for s, t, rel, f, ln, _a in g.out.get(c, ()):
            if rel in ("calls", "instantiates") and t in added_ids and t != c and t in g.nodes \
                    and g.nodes[t]["type"] in CALLABLE and not g.is_test(c) \
                    and g.nodes[c]["file"] != g.nodes[t]["file"]:   # internals of the helper file are not entry points
                helpers[t].add(c)
    if not helpers:
        return []
    # one family per helper file: `TagCanonicalization.Canonical` and
    # `.Canonicalize` are the same helper for the purpose of "who skipped it"
    fam_of = {}
    for h in list(helpers):
        f = g.nodes[h]["file"]
        if f in fam_of:
            helpers[fam_of[f]] |= helpers.pop(h)
        else:
            fam_of[f] = h
    fam_members = {f: {nid for nid, n in g.nodes.items() if n["file"] == f} for f in fam_of}
    # Concept of a helper: the domain words of its name and owner, minus the
    # verb (`TagCanonical.CanonicalList` / `normalizeTag` -> {"tag"}).
    files: dict = {}

    def body_of(n):
        if n["file"] not in files:
            files[n["file"]] = None
            for r in roots:
                p = Path(r) / n["file"]
                if p.is_file():
                    try:
                        files[n["file"]] = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    except OSError:
                        pass
                    break
        lines = files[n["file"]]
        if not lines:
            return ""
        return "\n".join(ln for ln in lines[max(0, n["line"] - 1):max(n["line"], n["end"])]
                         if not _COMMENT.match(ln))

    touched_files = {g.nodes[c]["file"] for c in changed_ids if c in g.nodes}
    gaps = []
    for h, callers in helpers.items():
        hn = g.nodes[h]
        # the helper's own name first (`restore`, `normalizeTag`); its owner
        # only when the name is all verb (`TagCanonical.CanonicalList`)
        parts = hn["qn"].replace("-", ".").split(".")
        concept = {w.lower() for w in _words(parts[-1])} - _GENERIC
        if not {w for w in concept if len(w) >= 3} and len(parts) > 1:
            concept = {w.lower() for w in _words(parts[-2])} - _GENERIC
        concept = {w for w in concept if len(w) >= 3}
        if not concept:
            continue
        members = fam_members.get(hn["file"], {h})
        hcallers = {s for m in members for s, t, rel, *_ in g.inc.get(m, ()) if rel in ("calls", "instantiates")}
        # what the helper's callers call next already receives canonical data
        downstream = {t for c in hcallers for _s, t, rel, *_ in g.out.get(c, ()) if rel == "calls"}
        ext = Path(hn["file"]).suffix
        same_lang = {".ts": (".ts", ".tsx"), ".tsx": (".ts", ".tsx"), ".js": (".js", ".jsx", ".mjs")}.get(ext, (ext,))
        cands = []
        for nid, n in g.nodes.items():
            if n["type"] not in ("function", "method") or nid in hcallers or nid == h or g.is_test(nid) \
                    or nid in downstream or Path(n["file"]).suffix not in same_lang or n["file"] == hn["file"] \
                    or _GENERATED.search(n["file"]) or n["label"] in _SCHEMA_METHODS:
                continue
            body = body_of(n)
            if not body:
                continue
            ids = {t for t in _IDENT_ANY.findall(body) if t not in _STOP}
            hit = sorted(t for t in ids if concept & {w.lower() for w in _words(t)})
            if len(hit) < 2:
                continue
            cands.append((len(hit), nid, hit))
        # a missed place usually sits in a file the change did not touch at all
        cands.sort(key=lambda x: (g.nodes[x[1]]["file"] in touched_files, -x[0], g.nodes[x[1]]["file"]))
        for score, nid, hit in cands[:max_items]:
            n = g.nodes[nid]
            gaps.append({"helper": hn["qn"], "used_by": sorted(g.nodes[c]["qn"] for c in callers)[:4],
                         "candidate": n["qn"], "at": f"{n['file']}:{n['line']}",
                         "shares": hit[:6], "changed": nid in changed_ids})
    touched = {g.nodes[c]["file"] for c in changed_ids if c in g.nodes}
    gaps.sort(key=lambda x: x["at"].rsplit(":", 1)[0] in touched)
    return gaps[:max_items]


_GENERIC = {"canonical", "canonicalize", "canonicalise", "normalize", "normalise", "normalized", "format",
            "formatter", "parse", "parser", "sanitize", "sanitise", "clean", "to", "get", "set", "list", "helper",
            "helpers", "util", "utils", "string", "str", "value", "values", "validate", "validator", "convert",
            "make", "build", "create", "from", "of", "for", "apply", "map", "compute", "is", "has", "check",
            "ensure", "safe", "default", "new", "with", "by", "all", "one", "item", "items", "service",
            "handle", "handler", "query", "command", "request", "response", "controller", "constructor",
            "execute", "invoke", "process", "async", "model", "dto", "impl", "base", "manager", "run", "main"}
# schema / model configuration: names the data, never transforms it
_SCHEMA_METHODS = {"OnModelCreating", "Configure", "BuildModel", "BuildTargetModel", "Up", "Down",
                   "upgrade", "downgrade", "configure", "define_tables", "create_table"}
_IDENT_ANY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GENERATED = re.compile(r"(^|/)(Migrations|migrations|generated|__generated__)/|\.Designer\.cs$|ModelSnapshot\.cs$|\.g\.cs$")


def _words(ident: str) -> list:
    return re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", ident)


def _schema_gaps(cx, roots, rels: set, added_text: dict) -> list:
    """Code outside the change that still uses a column/table that a changed
    migration drops or renames (`ALTER TABLE tasks RENAME COLUMN status_id
    TO state_id` while `Task.php` still says `t.status_id`)."""
    out, seen = [], set()
    for rel in sorted(rels):
        # `.sql` DDL and code migrations (Laravel, Rails, Django, Alembic, EF) alike
        row = cx.execute("SELECT attributes, repository_id FROM nodes WHERE type='file' AND file_path=?",
                         (rel,)).fetchone()
        if not row:
            continue
        try:
            drops = (json.loads(row[0] or "{}") or {}).get("drops") or []
        except ValueError:
            drops = []
        added = " ".join(added_text.get(rel, []))
        for d in drops:
            what = d.get("column") or d.get("table") or ""
            name = what.rsplit(".", 1)[-1]
            if not name or (added and name not in added):
                continue          # only what this change itself writes
            if d.get("column"):
                tname, cname = what.rsplit(".", 1)
                tgt = cx.execute("SELECT id FROM nodes WHERE type='column' AND lower(qualified_name)=lower(?) "
                                 "AND repository_id=?", (what, row[1])).fetchone()
            else:
                tgt = cx.execute("SELECT id FROM nodes WHERE type='table' AND lower(label)=lower(?) "
                                 "AND repository_id=?", (what, row[1])).fetchone()
            if not tgt:
                continue
            how = (f"renamed to {d['renamed_to']}" if d.get("renamed_to") else "dropped") + f" by {rel}:{d.get('line')}"
            for f, ln, attrs, src in cx.execute(
                    "SELECT e.file_path, e.start_line, e.attributes, n.qualified_name FROM edges e "
                    "JOIN nodes n ON n.id=e.source WHERE e.target=? AND e.method='sql-link' "
                    "AND json_extract(e.attributes,'$.via') IN ('sql-literal','orm')", (tgt[0],)):
                if f in rels:
                    continue
                for line in occurrence_lines(ln or 0, attrs):
                    site = site_line(roots, f, line, name, window=0)
                    if site is None or (f, site[0], what) in seen:
                        continue
                    seen.add((f, site[0], what))
                    out.append({"schema": what, "kind": "column" if d.get("column") else "table",
                                "why": how, "user": src, "at": f"{f}:{site[0]}", "code": site[1]})
    return out


def _summary(r: dict) -> str:
    parts = [f"{len(r['files'])} files, {len(r['changed_symbols'])} changed symbols"]
    if r["removed"]:
        parts.append(f"{len(r['removed'])} removed/renamed")
    if r["signature_changes"]:
        parts.append(f"{len(r['signature_changes'])} signature changes")
    if r["stale_callers"]:
        parts.append(f"{len(r['stale_callers'])} callers outside the change still use an old shape")
    if r.get("schema_gaps"):
        parts.append(f"{len(r['schema_gaps'])} places still use a column/table your migration drops or renames")
    if r["dangling_calls"]:
        parts.append(f"{len(r['dangling_calls'])} calls that resolve to nothing")
    if r.get("replacement_gaps"):
        parts.append(f"{len(r['replacement_gaps'])} places still calling what you replaced")
    if r["pattern_gaps"]:
        parts.append(f"{len(r['pattern_gaps'])} possible (unverified) places for the new helper")
    if r.get("rule_violations"):
        parts.append(f"{len(r['rule_violations'])} architecture rule violations")
    if r.get("unrelated_changes"):
        parts.append(f"{len(r['unrelated_changes'])} changed files unrelated to the task")
    parts.append(f"{r['dependents_total']} dependents, {r['tests']['total']} tests reach the change")
    return "; ".join(parts) + "."


def render_text(r: dict) -> str:
    """Compact plain-text report for agents (MCP) and the CLI."""
    if r.get("error"):
        return "check_change: " + r["error"]
    out = [f"# check_change vs {r['base']}: {r.get('summary', '')}"]
    if not r.get("changed_symbols") and not r.get("files"):
        return out[0]

    def sec(title, rows):
        if rows:
            out.append(f"\n## {title}")
            out.extend(rows)
    shown = 15
    st_ = r["stale_callers"]
    sec(f"DEFINITE - callers outside your change that still use the old shape ({len(st_)})",
        [f"- {s['at']} -> {s['symbol']} ({s['why']})\n    code: {s.get('code', '')}" for s in st_[:shown]]
        + ([f"Also, same problem ({len(st_) - shown} more):"] + rest_by_file(st_[shown:]) if len(st_) > shown else []))
    rp_ = r.get("replacement_gaps", [])
    sec(f"DEFINITE - places that still call what your change replaced elsewhere ({len(rp_)})",
        [f"- {x['at']} still calls {x['old']} (you switched {', '.join(x['switched'])} to {x['new']})\n"
         f"    code: {x['code']}" for x in rp_[:shown]]
        + ([f"Also, same problem ({len(rp_) - shown} more):"] + rest_by_file(rp_[shown:]) if len(rp_) > shown else []))
    sg_ = r.get("schema_gaps") or []
    sec(f"DEFINITE - code still using a column/table your migration drops or renames ({len(sg_)})",
        [f"- {x['at']} uses {x['schema']} ({x['why']})\n    code: {x['code']}" for x in sg_[:shown]]
        + ([f"Also, same problem ({len(sg_) - shown} more):"] + rest_by_file(sg_[shown:]) if len(sg_) > shown else []))
    sec("DEFINITE - calls in your new code that resolve to nothing in the repo",
        [f"- {d['at']} {d['call']} (closest: {d['did_you_mean']})\n    code: {d.get('code', '')}"
         for d in r["dangling_calls"]])
    sec("POSSIBLE (word similarity only, verify before changing) - same data as your new helper, no call to it",
        [f"- {p['candidate']} @ {p['at']} - shares {', '.join(p['shares'])}; "
         f"{p['helper'].rsplit('.', 1)[-1]} is used by {', '.join(x.rsplit('.', 2)[-2] + '.' + x.rsplit('.', 1)[-1] for x in p['used_by'])}"
         + (" (in your change)" if p["changed"] else "") for p in r["pattern_gaps"]])
    rv_ = r.get("rule_violations") or []
    sec(f"DEFINITE - architecture rules this change breaks ({len(rv_)})",
        [f"- {v['file']}:{v['line']} [{v['rule']}] {v['message']}" for v in rv_[:shown]])
    if r.get("rules_error"):
        sec("Rules file error", [f"- {r['rules_error']}"])
    ag_ = r.get("access_gaps_elsewhere") or []
    sec(f"NOTE - access gaps elsewhere in the repository, not from this change ({len(ag_)})",
        [f"- {v['file']}:{v['line']} {v['message']}" for v in ag_])
    uc_ = r.get("unrelated_changes") or []
    sec(f"CHECK - changed files the task does not lead to ({len(uc_)}); intended?",
        [f"- {u['file']} ({u['status']})" for u in uc_])
    sec("Signature changes", [f"- {s['symbol']}: params {s['before']} -> {s['after']}" for s in r["signature_changes"]])
    sec("Frontend/other-language callers of changed endpoints",
        [f"- {h['http']}: {h['caller']} @ {h['at']} -> {h['handler']}" for h in r["http_callers"]])
    sec(f"Dependents outside the change ({r['dependents_total']})",
        [f"- {d['symbol']} @ {d['at']}" for d in r["dependents"][:15]])
    t = r["tests"]
    sec(f"Tests that reach the change ({t['total']})",
        [f"- {x['file']}: {', '.join(x['tests'])}" for x in t["files"][:20]] + (["Run:"] + [f"  {c}" for c in t["commands"]] if t["commands"] else []))
    sec("Changed symbols", [f"- {c['symbol']} @ {c['at']}" for c in r["changed_symbols"][:20]])
    return "\n".join(out)
