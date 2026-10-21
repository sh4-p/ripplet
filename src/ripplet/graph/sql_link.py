"""Code <-> database schema links.

The schema comes from `.sql` files (extraction/structured/sql.py): tables,
columns, foreign keys, views. This pass connects it to the code that uses
it, so "what uses table `tasks`" / "who reads `tasks.status_id`" have
answers and a migration that drops or renames a column can be checked
against its users:

  * code -> table   `references` (via sql-literal / orm), every line kept
    in `occurrences`: a table named after FROM / JOIN / INTO / UPDATE /
    TABLE inside a string literal that reads as SQL, or in an ORM mapping
    (`DB::table('tasks')`, `->from('x')`, `[Table("x")]`, `.ToTable("x")`,
    `__tablename__ = "x"`, `db_table = "x"`, `@Table(name = "x")`,
    `protected $table = 'x'`)
  * code -> column  `references`, for column names written in a literal
    that references the column's table (`UPDATE tasks SET status_id = ?`)
  * table -> table  `references` for foreign keys and view sources, when
    the referenced table is in the schema

Only tables the schema defines are linked: prose that happens to contain
"from the list" never matches anything. Recomputed on every index run.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from ..core.identifiers import canonical_edge_id
from ..core.models import Edge

METHOD = "sql-link"
VERSION = "1"
CODE_EXT = {".php", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".cs", ".go",
            ".rb", ".rs", ".swift", ".dart", ".scala", ".lua", ".cpp", ".c", ".h", ".hpp", ".vue", ".svelte"}
_SQL_VERB = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|REPLACE|MERGE|UPSERT|WITH|CREATE|ALTER|DROP|TRUNCATE)\b", re.I)
_TABLE_AT = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE|EXISTS)\s+[`\"\[]?([A-Za-z_][\w$]*(?:[`\"\]]?\.[`\"\[]?[A-Za-z_][\w$]*)?)", re.I)
_ORM = [
    re.compile(r"(?:DB::table|->table|::table|->from|->join|->leftJoin|->rightJoin|\bknex|\.from|\.table|\.into)\s*\(\s*['\"]([A-Za-z_][\w$.]*)['\"]"),
    re.compile(r"\[\s*Table\s*\(\s*\"([A-Za-z_][\w$.]*)\""),
    re.compile(r"\.ToTable\s*\(\s*\"([A-Za-z_][\w$.]*)\""),
    re.compile(r"__tablename__\s*=\s*['\"]([A-Za-z_][\w$.]*)['\"]"),
    re.compile(r"\bdb_table\s*=\s*['\"]([A-Za-z_][\w$.]*)['\"]"),
    re.compile(r"@Table\s*\(\s*(?:name\s*=\s*)?\"([A-Za-z_][\w$.]*)\""),
    re.compile(r"(?:protected|public|private)\s+(?:static\s+)?(?:string\s+)?\$table\s*=\s*['\"]([A-Za-z_][\w$.]*)['\"]"),
]
_WORD = re.compile(r"[A-Za-z_][\w$]*")
_ALIAS = re.compile(r"[`\"\]]?\s+(?:AS\s+)?([A-Za-z_]\w*)", re.I)
_QUALIFIER = re.compile(r"([A-Za-z_]\w*)\s*\.\s*$")
_NOT_ALIAS = {"where", "on", "join", "left", "right", "inner", "outer", "cross", "full", "set", "values",
              "select", "group", "order", "limit", "having", "union", "using", "natural", "as", "and", "or",
              "returning", "into", "default", "partition", "window", "for", "lock", "straight_join"}


def string_literals(text: str, lang_ext: str):
    """(start offset, body) for every string literal, multi-line ones
    included; comments skipped. Python/Dart triple quotes, JS/Go
    backticks, PHP/Ruby heredocs are bodies too."""
    out = []
    i, n = 0, len(text)
    hash_comments = lang_ext in (".py", ".rb", ".php", ".sh")
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/" and lang_ext != ".py":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "#" and hash_comments:
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "<" and lang_ext == ".php" and text.startswith("<<<", i):
            m = re.match(r"<<<\s*['\"]?([A-Za-z_]\w*)['\"]?\r?\n", text[i:])
            if m:
                tag = m.group(1)
                body_at = i + m.end()
                end = re.search(r"^\s*" + re.escape(tag) + r"\b", text[body_at:], re.M)
                stop = body_at + (end.start() if end else len(text) - body_at)
                out.append((body_at, text[body_at:stop]))
                i = stop + (len(end.group(0)) if end else 0)
                continue
        if c in ("'", '"', "`"):
            triple = text.startswith(c * 3, i) and lang_ext in (".py", ".dart", ".kt", ".java", ".swift", ".scala")
            q = c * 3 if triple else c
            j = i + len(q)
            while j < n:
                if text[j] == "\\" and lang_ext not in (".cs",):
                    j += 2
                    continue
                if text.startswith(q, j):
                    break
                if not triple and c != "`" and text[j] == "\n" and lang_ext not in (".php", ".rb", ".sql"):
                    break                       # unterminated single-line string
                j += 1
            out.append((i + len(q), text[i + len(q):j]))
            i = j + len(q)
            continue
        i += 1
    return out


def link_sql(store, repo_roots: dict) -> dict:
    cx = store.cx
    old = [r[0] for r in cx.execute("SELECT id FROM edges WHERE method=?", (METHOD,))]
    for i in range(0, len(old), 500):
        ch = old[i:i + 500]
        q = ",".join("?" * len(ch))
        cx.execute(f"DELETE FROM edges WHERE id IN ({q})", ch)
        cx.execute(f"DELETE FROM file_edges WHERE edge_id IN ({q})", ch)
    tables: dict = {}            # (repo, lower name) -> table id
    columns: dict = defaultdict(dict)   # table id -> {lower col: col id}
    table_attrs: dict = {}
    for nid, t, lab, rid, attrs, fp in cx.execute(
            "SELECT id, type, label, repository_id, attributes, file_path FROM nodes WHERE type IN ('table','column')"):
        if t == "table":
            tables[(rid, (lab or "").lower())] = nid
            try:
                table_attrs[nid] = (rid, json.loads(attrs or "{}"), fp)
            except ValueError:
                table_attrs[nid] = (rid, {}, fp)
    for cid, lab, attrs, rid in cx.execute(
            "SELECT id, label, attributes, repository_id FROM nodes WHERE type='column'"):
        try:
            a = json.loads(attrs or "{}")
        except ValueError:
            a = {}
        tid = tables.get((rid, str(a.get("table", "")).lower()))
        if tid:
            columns[tid][(lab or "").lower()] = cid
    if not tables:
        return {"edges": 0, "tables": 0}
    new: list = []
    by_key: dict = {}

    def add(src, tgt, fp, line, via, rid, extra=None):
        key = (src, tgt, fp)
        e = by_key.get(key)
        if e is None:
            e = Edge(canonical_edge_id(src, "references", tgt, fp, "sql"), src, tgt, "references", "resolved",
                     "resolved", 0.8, [], METHOD, VERSION, file_path=fp, start_line=line,
                     attributes=dict({"via": via, "occurrences": []}, **(extra or {})))
            by_key[key] = e
            new.append((e, f"{rid}:{fp}"))
        occ = e.attributes["occurrences"]
        if all(o["line"] != line for o in occ):
            occ.append({"line": line, "end_line": line, "col": None})
            occ.sort(key=lambda o: o["line"])
            e.start_line = occ[0]["line"]

    # 1. schema-internal: foreign keys and view sources
    for tid, (rid, a, fp) in table_attrs.items():
        for fk in a.get("foreign_keys") or []:
            rt = tables.get((rid, str(fk.get("table", "")).lower()))
            if rt and rt != tid:
                add(tid, rt, fp or "", fk.get("line") or 0, "foreign-key", rid,
                    {"columns": fk.get("columns"), "ref_columns": fk.get("ref_columns")})
                for c, rc in zip(fk.get("columns") or [], fk.get("ref_columns") or []):
                    a_, b_ = columns[tid].get(str(c).lower()), columns[rt].get(str(rc).lower())
                    if a_ and b_:
                        add(a_, b_, fp or "", fk.get("line") or 0, "foreign-key", rid)
        for src in a.get("view_sources") or []:
            rt = tables.get((rid, str(src).lower()))
            if rt and rt != tid:
                add(tid, rt, fp or "", 0, "view-source", rid)

    # 2. code -> tables/columns
    spans = defaultdict(list)
    module_of = {}
    for nid, t, fp, sl, el, rid in cx.execute(
            "SELECT id, type, file_path, start_line, end_line, repository_id FROM nodes "
            "WHERE type IN ('function','method','test','class','struct','record','interface','module','trait')"):
        if not fp or Path(fp).suffix.lower() not in CODE_EXT:
            continue
        if t == "module":
            module_of[(rid, fp)] = nid
        else:
            spans[(rid, fp)].append((sl or 0, el or sl or 0, nid))
    roots = dict(repo_roots or {})
    repo_tables = defaultdict(dict)
    for (rid, name), tid in tables.items():
        repo_tables[rid][name] = tid
    files = set(spans) | set(module_of)
    stats = {"files": 0, "table_refs": 0, "column_refs": 0}
    for rid, fp in sorted(files):
        names = repo_tables.get(rid)
        root = roots.get(rid)
        if not names or not root:
            continue
        try:
            text = (Path(root) / fp).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        low = text.lower()
        if not any(n in low for n in names):
            continue
        stats["files"] += 1
        sp = sorted(spans.get((rid, fp), []), key=lambda x: (x[0], -x[1]))
        line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]

        def line_of(off: int) -> int:
            lo, hi = 0, len(line_starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if line_starts[mid] <= off:
                    lo = mid
                else:
                    hi = mid - 1
            return lo + 1

        def owner_at(line: int):
            own = None
            for a, b, nid in sp:
                if a <= line <= b:
                    own = nid            # innermost: spans sorted outer-first
            return own or module_of.get((rid, fp))

        ext = Path(fp).suffix.lower()
        for off, body in string_literals(text, ext):
            if len(body) < 6 or not _SQL_VERB.search(body):
                continue
            used: dict = {}
            alias: dict = {}         # `FROM tasks t` / `JOIN users AS u` -> table id
            for m in _TABLE_AT.finditer(body):
                nm = re.split(r"[`\"\]]?\.[`\"\[]?", m.group(1))[-1].lower()
                tid = names.get(nm)
                if tid:
                    used.setdefault(tid, []).append(off + m.start(1))
                    alias[nm] = tid
                    am = _ALIAS.match(body, m.end())
                    if am and am.group(1).lower() not in _NOT_ALIAS:
                        alias[am.group(1).lower()] = tid
            if not used:
                continue
            for tid, offs in used.items():
                for o in offs:
                    line = line_of(o)
                    own = owner_at(line)
                    if own:
                        add(own, tid, fp, line, "sql-literal", rid)
                        stats["table_refs"] += 1
            # columns of the referenced tables written in the same statement
            col_owner = defaultdict(list)
            for tid in used:
                for cname, cid in columns.get(tid, {}).items():
                    col_owner[cname].append(cid)
            for wm in _WORD.finditer(body):
                w = wm.group(0).lower()
                cids = col_owner.get(w)
                if not cids or w in names:
                    continue
                qual = _QUALIFIER.search(body[max(0, wm.start() - 80):wm.start()])
                if qual:
                    # `t.id`: the alias decides the table; an alias this
                    # statement does not define (outer query) links nothing
                    tq = alias.get(qual.group(1).lower())
                    cids = [c for c in cids if tq and c in columns.get(tq, {}).values()]
                    if not cids:
                        continue
                line = line_of(off + wm.start())
                own = owner_at(line)
                if not own:
                    continue
                if len(cids) == 1:
                    add(own, cids[0], fp, line, "sql-literal", rid)
                    stats["column_refs"] += 1
        for i, ln in enumerate(text.splitlines(), 1):
            for rx in _ORM:
                for m in rx.finditer(ln):
                    tid = names.get(m.group(1).rsplit(".", 1)[-1].lower())
                    own = owner_at(i) if tid else None
                    if tid and own:
                        add(own, tid, fp, i, "orm", rid)
                        stats["table_refs"] += 1
    if new:
        store.upsert_edges([e for e, _ in new])
        for e, fk in new:
            cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (fk, e.id))
    stats["edges"] = len(new)
    stats["tables"] = len(tables)
    return stats
