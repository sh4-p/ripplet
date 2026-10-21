"""change_plan: every place a rename / signature change / removal of one
symbol must touch, BEFORE the edit is made.

An agent that renames with grep gets three kinds of lines mixed together:
real uses, unrelated code with the same name, and text (strings, docs).
It also misses overrides and interface members that must change in step.
This answers from the graph instead:

  DECLARATIONS   the symbol and every override / interface member bound to
                 it by dispatch (change them together or the code breaks)
  CALL SITES     each line that names it, per call (not one per caller),
                 grouped by file, tests marked
  NO EDIT        callers that reach it without writing its name (DI,
                 framework dispatch): nothing to rename, but they run it
  SAME NAME, OTHER SYMBOL   lines that call a different symbol of the same
                 name: leave them alone
  TEXT MENTIONS  strings, comments, docs naming it: not proven references,
                 change only where they mean this symbol

Every entry carries file:line and the code on that line.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from .check import occurrence_lines, site_line

_MEMBER = ("method", "function", "test")
_TYPES = ("class", "interface", "struct", "record", "enum", "type", "trait", "object")
_SCHEMA = ("table", "column")
_CODE = _MEMBER + _TYPES + _SCHEMA
_USE_RELS = ("calls", "instantiates", "references", "inherits", "implements", "imports", "calls_http")
_TEST_RX = re.compile(r"(^|/)(tests?|spec|__tests__)/|(_test|_spec|\.test|\.spec|Tests?)\.[a-z]+$|"
                      r"(^|/)test_[^/]*$", re.I)
CHANGES = ("rename", "signature", "remove")


def _rows(cx, sql, args=()):
    cx.row_factory = __import__("sqlite3").Row
    return cx.execute(sql, args).fetchall()


def _resolve(cx, ref: str):
    """(node row, other candidates). Accepts an id, a qualified name, a
    `Class.member` suffix, or a bare name."""
    r = _rows(cx, "SELECT * FROM nodes WHERE id=?", (ref,))
    if r:
        return r[0], []
    q = ",".join("?" * len(_CODE))
    cands = _rows(cx, f"SELECT * FROM nodes WHERE type IN ({q}) AND (qualified_name=? OR label=? "
                      f"OR qualified_name LIKE ?)", (*_CODE, ref, ref.split(".")[-1], "%." + ref))
    if not cands:
        return None, []
    def rank(n):
        qn = n["qualified_name"] or ""
        exact = 0 if qn == ref else 1 if qn.endswith("." + ref) else 2
        return (exact, bool(_TEST_RX.search(n["file_path"] or "")), len(qn))
    cands = sorted(cands, key=rank)
    return cands[0], cands[1:]


def _family(cx, node) -> list:
    """The symbol plus every same-named member tied to it by dispatch
    (base declaration <-> overrides/implementations), transitively."""
    if node["type"] not in _MEMBER:
        return [node]
    label = node["label"]
    seen = {node["id"]: node}
    todo = [node["id"]]
    while todo:
        nid = todo.pop()
        for r in _rows(cx, "SELECT e.source, e.target FROM edges e WHERE e.relation='dispatches' "
                           "AND (e.source=? OR e.target=?)", (nid, nid)):
            other = r["target"] if r["source"] == nid else r["source"]
            if other in seen:
                continue
            n = _rows(cx, "SELECT * FROM nodes WHERE id=?", (other,))
            if n and n[0]["label"] == label and n[0]["type"] in _MEMBER:
                seen[other] = n[0]
                todo.append(other)
    return list(seen.values())


def _incoming(cx, ids: list) -> list:
    q = ",".join("?" * len(ids))
    r = ",".join("?" * len(_USE_RELS))
    return _rows(cx, f"SELECT e.source, e.target, e.relation, e.file_path, e.start_line, e.attributes, "
                     f"n.qualified_name sq, n.type st FROM edges e LEFT JOIN nodes n ON n.id=e.source "
                     f"WHERE e.target IN ({q}) AND e.relation IN ({r}) "
                     f"AND e.id NOT IN (SELECT edge_id FROM superseded_edges)", (*ids, *_USE_RELS))


def _sites(cx, roots, ids, name):
    """(named, unnamed): named = {(file, line): (code, caller qn, relation, arity)}."""
    named, unnamed = {}, {}
    for e in _incoming(cx, ids):
        f = (e["file_path"] or "").replace("\\", "/")
        if not f:
            continue
        try:
            arity = json.loads(e["attributes"] or "{}").get("arity")
        except ValueError:
            arity = None
        hit = False
        for ln in occurrence_lines(e["start_line"] or 0, e["attributes"]):
            site = site_line(roots, f, ln, name)
            if site is None:
                continue
            hit = True
            named.setdefault((f, site[0]), (site[1], e["sq"] or e["source"], e["relation"], arity))
        if not hit and e["relation"] != "imports":
            unnamed.setdefault((f, e["start_line"] or 0), (e["sq"] or e["source"], e["relation"]))
    return named, unnamed


def _code_at(roots, rel, line) -> str:
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


_SQL_WORDS = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|WHERE|ORDER\s+BY|GROUP\s+BY|JOIN|IS\s+NULL|"
                        r"IS\s+NOT\s+NULL|COALESCE|ALTER\s+TABLE|CREATE\s+TABLE)\b", re.I)
_INPUT_SRC = re.compile(r"(Request::\w+|\$request->\w+|request\.(form|json|args|get|data)|req\.(body|query|params)|"
                        r"\$_(POST|GET|REQUEST)|\$(body|args|input|params|payload|data_in)\b|"
                        r"\bname\s*=\s*[\"'])", re.I)


def _column_groups(name: str, mentions: list) -> dict:
    """Split a column name's text mentions by role (see change_plan)."""
    q = r"[\"']" + re.escape(name) + r"[\"']"
    row_rx = re.compile(r"\$\w+\s*\[\s*" + q + r"\s*\]|\b\w+\." + re.escape(name) + r"\b|\[\s*" + q + r"\s*\]")
    key_rx = re.compile(q + r"\s*(=>|:)")
    sql_frag = re.compile(r"[\"'][^\"']*\b" + re.escape(name) + r"\s*(=\s*\?|=\s*:\w+|\bIS\b|\bASC\b|\bDESC\b|,)", re.I)
    client = re.compile(r"\b(form|payload|data|body|params|this\.form|item|task|row)\." + re.escape(name) + r"\b")
    model_in = re.compile(r"\$(data|attrs|attributes|fields|values)\s*\[\s*" + q)
    out: dict = {}
    for f, ln, code in mentions:
        if f.endswith(".sql") or sql_frag.search(code):
            out.setdefault("sql", []).append((f, ln, code))
            continue
        if model_in.search(code) or (f.endswith((".php", ".js", ".ts", ".vue", ".jsx", ".tsx", ".html"))
                                     and client.search(code) and not code.lstrip().startswith("$")):
            out.setdefault("input", []).append((f, ln, code))
            continue
        if f.endswith((".md", ".txt", ".rst")) or code.lstrip().startswith(("//", "#", "*", "/*", "--")) \
                and not _SQL_WORDS.search(code):
            g = "other"
        elif _INPUT_SRC.search(code) and (re.search(r"(Request::\w+|\$_(POST|GET|REQUEST)|req\.\w+|request\.\w+)\s*"
                                                   r"[\(\[]\s*" + q, code)
                                          or re.search(r"\$(body|args|input|params|payload)\s*\[\s*" + q, code)
                                          or re.search(r"\bname\s*=\s*[\"']" + re.escape(name), code)):
            g = "input"
        elif _SQL_WORDS.search(code) and not key_rx.search(code):
            g = "sql"
        elif key_rx.search(code):
            g = "output"
        elif row_rx.search(code):
            g = "row"
        else:
            g = "other"
        out.setdefault(g, []).append((f, ln, code))
    return out


def _text_mentions(cx, roots, name, skip: set, limit: int):
    rx = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    files = [r[0] for r in cx.execute("SELECT DISTINCT file_path FROM nodes WHERE file_path<>''")]
    out, total = [], 0
    for rel in sorted(files):
        rel = rel.replace("\\", "/")
        for root in roots:
            p = Path(root) / rel
            if not p.is_file():
                continue
            try:
                if p.stat().st_size > 1_500_000:
                    break
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                break
            if name not in text:
                break
            for i, line in enumerate(text.splitlines(), 1):
                if (rel, i) in skip or not rx.search(line):
                    continue
                total += 1
                if len(out) < limit:
                    t = line.strip()
                    out.append((rel, i, t if len(t) <= 140 else t[:137] + "..."))
            break
    return out, total


def change_plan(store, cfg, symbol: str, change: str = "rename", new_name: str = "",
                include_text: bool = True) -> str:
    change = change if change in CHANGES else "rename"
    cx = store.cx
    roots = [str(r) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    node, others = _resolve(cx, symbol)
    if node is None:
        return (f"change_plan: no code symbol named {symbol!r} in the index. "
                f"Use search_symbols to find the exact name.")
    name = node["label"]
    fam = _family(cx, node)
    fam_ids = {n["id"] for n in fam}
    named, unnamed = _sites(cx, roots, list(fam_ids), name)
    decl_lines = {((n["file_path"] or "").replace("\\", "/"), n["start_line"]) for n in fam}
    for k in list(named):
        if k in decl_lines:
            named.pop(k)
    # same name, different symbol: their sites are excluded and listed
    same = [o for o in others if o["label"] == name and o["id"] not in fam_ids]
    other_sites = {}
    if same:
        o_named, _ = _sites(cx, roots, [o["id"] for o in same], name)
        for k, v in o_named.items():
            if k not in named:
                other_sites[k] = v
    for o in same:
        k = ((o["file_path"] or "").replace("\\", "/"), o["start_line"])
        other_sites.setdefault(k, (_code_at(roots, *k), o["qualified_name"], "declaration", None))

    out = []
    tests = sum(1 for (f, _l) in named if _TEST_RX.search(f))
    files = {f for f, _ in named} | {(n["file_path"] or "").replace("\\", "/") for n in fam}
    verb = {"rename": f"rename {name}" + (f" -> {new_name}" if new_name else ""),
            "signature": f"change the signature of {name}", "remove": f"remove {name}"}[change]
    out.append(f"# change_plan: {verb}")
    out.append(f"Symbol: {node['qualified_name']} ({node['type']}) at {node['file_path']}:{node['start_line']}")
    out.append(f"{len(fam)} declaration(s) and {len(named)} call site(s) ({tests} in tests) "
               f"across {len(files)} file(s).")
    if change == "rename":
        out.append("Edit every DECLARATION and CALL SITE line below; nothing else is proven to refer to it.")
        out.append(f"Faster: rename_symbol(symbol='{node['qualified_name']}', new_name=...) applies exactly "
                   "these edits in one call, without opening the files.")
    elif change == "signature":
        out.append("Update every DECLARATION together, then every CALL SITE to the new parameters. "
                   "Arity shown where known.")
    else:
        out.append("Every CALL SITE must stop using it before the DECLARATIONS are deleted.")
    out.append("After editing, call check_change: it confirms nothing was left behind.")
    out.append("")
    out.append(f"=== DECLARATIONS ({len(fam)}){' - change together' if len(fam) > 1 else ''}")
    for n in sorted(fam, key=lambda n: (n["file_path"] or "", n["start_line"] or 0)):
        f = (n["file_path"] or "").replace("\\", "/")
        role = "this symbol" if n["id"] == node["id"] else "override / implementation in the same family"
        out.append(f"{f}:{n['start_line']}  [{role}]  {_code_at(roots, f, n['start_line'])}")
    out.append("")
    out.append(f"=== CALL SITES ({len(named)})")
    by_file = defaultdict(list)
    for (f, ln), v in named.items():
        by_file[f].append((ln, v))
    for f in sorted(by_file, key=lambda f: (bool(_TEST_RX.search(f)), f)):
        tag = "  (test)" if _TEST_RX.search(f) else ""
        out.append(f"-- {f}{tag}")
        for ln, (code, caller, rel, arity) in sorted(by_file[f]):
            extra = f"  [{rel}]" if rel not in ("calls",) else ""
            if change == "signature" and arity is not None:
                extra += f"  [{arity} arg(s)]"
            out.append(f"  {ln}: {code}{extra}")
    if unnamed:
        out.append("")
        out.append(f"=== NO EDIT: reach it without writing its name ({len(unnamed)})")
        out.append("They run it (framework, DI, dispatch). A rename needs no edit here; "
                   "a signature change may.")
        for (f, ln), (caller, rel) in sorted(unnamed.items())[:20]:
            out.append(f"  {f}:{ln}  {caller}  [{rel}]")
    if other_sites:
        out.append("")
        out.append(f"=== SAME NAME, OTHER SYMBOL ({len(other_sites)}) - do not change")
        for (f, ln), (code, owner, rel, _a) in sorted(other_sites.items())[:25]:
            out.append(f"  {f}:{ln}  ({owner})  {code}")
    if include_text and node["type"] == "column":
        # a column's name also lives in code as a row field, a request
        # parameter and a response key: only the first follows the column
        skip = set(named) | set(other_sites) | decl_lines | set(unnamed)
        mentions, total = _text_mentions(cx, roots, name, skip, 2000)
        groups = _column_groups(name, mentions)
        titles = {
            "sql": "SQL TEXT naming it (queries the graph did not link: dynamic SQL, sort maps) - change",
            "row": "ROW FIELDS - a value read from a database row (`$task['x']`, `row.x`, `task.x`): follows "
                   "the column; change it, or keep the name by aliasing in the SELECT (`new AS x`)",
            "input": "INPUT NAMES - request/form/API parameters (`Request::post('x')`, `$body['x']`, "
                     "`name=\"x\"`): the outside contract; keep unless the task renames the API too",
            "output": "OUTPUT / ARRAY KEYS (`'x' => ...`): a response field or data handed to a model; "
                      "check which: response keys are the API, model input keys must match the column",
            "other": "OTHER TEXT (comments, docs, strings)",
        }
        for g in ("sql", "row", "input", "output", "other"):
            items = groups.get(g) or []
            if not items:
                continue
            out.append("")
            out.append(f"=== {titles[g]} ({len(items)})")
            for f, ln, code in items[:15]:
                out.append(f"  {f}:{ln}  {code}")
            if len(items) > 15:
                out.append(f"  ... {len(items) - 15} more: " + ", ".join(
                    sorted({f for f, _l, _c in items[15:]})[:8]))
    elif include_text:
        skip = set(named) | set(other_sites) | decl_lines | set(unnamed)
        from ..retrieval.uncertain import unverified_sites, render as _render_unv
        unv, unv_total = unverified_sites(cx, roots, fam_ids, name, skip=skip, limit=15)
        out += _render_unv(unv, unv_total, name)
        skip |= {(f, ln) for f, ln, _c, _r in unv}
        mentions, total = _text_mentions(cx, roots, name, skip, 15)
        if mentions:
            out.append("")
            out.append(f"=== TEXT MENTIONS ({total}) - strings, comments, docs, unindexed code; "
                       "not proven to be this symbol")
            out.append("Change one only where it clearly means this symbol (an error message, a doc "
                       "reference); leave the rest.")
            for f, ln, code in mentions:
                out.append(f"  {f}:{ln}  {code}")
            if total > len(mentions):
                out.append(f"  ... {total - len(mentions)} more")
    return "\n".join(out)
