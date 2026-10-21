"""SQL DDL extraction: tables, columns, foreign keys, views.

Every `.sql` file (schema dumps, numbered migrations) contributes to one
schema model per repository:

  CREATE TABLE [IF NOT EXISTS] t (col TYPE ..., FOREIGN KEY (c) REFERENCES u(id))
  ALTER TABLE t ADD [COLUMN] c TYPE / RENAME COLUMN a TO b / RENAME TO u
  CREATE [OR REPLACE] VIEW v AS SELECT ... FROM t JOIN u ...

Identity is the unqualified, lower-cased name (`"public"."Users"` and
`users` are one table), so a table created in one migration and altered
in another is a single node; each file records the facts it states.
`DROP COLUMN` / `DROP TABLE` are recorded on the file as `drops` (the
latest migration wins when tools read them), never by deleting nodes
another file produced.
"""
from __future__ import annotations

import re

from ...core.identifiers import content_hash, edge_id, file_node_id, stable_id
from ...core.models import Edge, ExtractionResult, Location, Node
from ...core.provenance import make_evidence

SQL_VERSION = "2"

_IDENT = r'(?:`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_QNAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT})*"
_CREATE_TABLE = re.compile(rf"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP(?:ORARY)?\s+)?"
                           rf"(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_QNAME})\s*\(", re.I | re.S)
_CREATE_VIEW = re.compile(rf"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                          rf"({_QNAME})(?:\s*\([^)]*\))?\s+AS\s+(.*)$", re.I | re.S)
_ALTER = re.compile(rf"^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?({_QNAME})\s+(.*)$", re.I | re.S)
_DROP_TABLE = re.compile(rf"^\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?({_QNAME}(?:\s*,\s*{_QNAME})*)", re.I)
_TABLE_REF = re.compile(rf"\b(?:FROM|JOIN|INTO|UPDATE)\s+({_QNAME})", re.I)
_CONSTRAINT_HEAD = re.compile(r"^\s*(?:CONSTRAINT\s+\S+\s+)?(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|KEY|INDEX|"
                              r"FULLTEXT|SPATIAL|CHECK|EXCLUDE)\b", re.I)
_FK = re.compile(rf"FOREIGN\s+KEY\s*\(([^)]*)\)\s*REFERENCES\s+({_QNAME})\s*(?:\(([^)]*)\))?", re.I)
_INLINE_FK = re.compile(rf"\bREFERENCES\s+({_QNAME})\s*(?:\(([^)]*)\))?", re.I)
_SQL_KEYWORDS = {"select", "from", "where", "table", "values", "set", "as", "on", "join", "into", "update"}


def _unquote(name: str) -> str:
    return name.strip().strip('`"[]')


def _short(qname: str) -> str:
    """`"public"."Users"` -> `Users`."""
    parts = re.split(r"\s*\.\s*", qname.strip())
    return _unquote(parts[-1]) if parts else ""


def table_id(repo_id: str, name: str) -> str:
    return stable_id("tbl", repo_id, name.lower())


def column_id(repo_id: str, table: str, col: str) -> str:
    return stable_id("col", repo_id, f"{table.lower()}.{col.lower()}")


def strip_comments(text: str) -> str:
    """Comments blanked with spaces (offsets and line numbers kept); quoted
    text left as is so a `--` inside a string is not a comment."""
    out = list(text)
    i, n = 0, len(text)
    quote = None
    while i < n:
        c = text[i]
        if quote:
            if c == quote:
                if i + 1 < n and text[i + 1] == quote:   # doubled quote escape
                    i += 2
                    continue
                quote = None
            elif c == "\\" and quote != '"':
                i += 1
            i += 1
            continue
        if c in ("'", '"', "`"):
            quote = c
        elif c == "-" and text.startswith("--", i) or c == "#" and (i == 0 or text[i - 1] in "\n\r\t "):
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        elif c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] not in "\r\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def split_statements(text: str):
    """(start offset, statement) split on top-level `;` (quotes respected)."""
    start, quote, depth = 0, None, 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote != '"':
                i += 1
        elif c in ("'", '"', "`"):
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == ";" and depth == 0:
            yield start, text[start:i]
            start = i + 1
        i += 1
    if text[start:].strip():
        yield start, text[start:]


def _top_level_items(body: str):
    """(offset, item) for the comma-separated items of a parenthesised list."""
    depth, quote, start = 0, None, 0
    for i, c in enumerate(body):
        if quote:
            if c == quote:
                quote = None
            continue
        if c in ("'", '"', "`"):
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            yield start, body[start:i]
            start = i + 1
    if body[start:].strip():
        yield start, body[start:]


def _paren_body(stmt: str, open_at: int) -> str:
    depth = 0
    for i in range(open_at, len(stmt)):
        if stmt[i] == "(":
            depth += 1
        elif stmt[i] == ")":
            depth -= 1
            if depth == 0:
                return stmt[open_at + 1:i]
    return stmt[open_at + 1:]


def _cols(spec: str) -> list:
    return [_unquote(x) for x in spec.split(",") if _unquote(x)]


def extract_sql(rel_path: str, text: str, repo_id: str, project_id: str) -> ExtractionResult:
    res = ExtractionResult(extractor="sql", extractor_version=SQL_VERSION)
    raw = text.encode("utf-8", errors="ignore")
    ev = make_evidence(rel_path, text[:300], "sql", raw)
    res.evidence.append(ev)
    fid = file_node_id(repo_id, rel_path)
    drops: list = []
    file_node = Node(fid, "file", rel_path, rel_path, project_id, repo_id, rel_path, Location(1, 0, 1, 0),
                     "sql", content_hash(raw),
                     {"parser": {"backend": "sql-ddl", "status": "ok", "language": "sql"}}, [ev.id],
                     schema_version=2)
    res.nodes.append(file_node)
    clean = strip_comments(text)
    line_at = [0]
    for i, ch in enumerate(clean):
        if ch == "\n":
            line_at.append(i + 1)

    def line_of(off: int) -> int:
        lo, hi = 0, len(line_at) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_at[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    seen_nodes: set = set()
    seen_edges: set = set()

    def node(nid, typ, label, qn, a, b, attrs):
        if nid in seen_nodes:
            return
        seen_nodes.add(nid)
        res.nodes.append(Node(nid, typ, label, qn, project_id, repo_id, rel_path, Location(a, 0, b, 0),
                              "sql", "", attrs, [ev.id], schema_version=2))

    def edge(src, rel, tgt, line, attrs=None):
        key = (src, rel, tgt)
        if key in seen_edges:
            return
        seen_edges.add(key)
        res.edges.append(Edge(edge_id(src, rel, tgt, line), src, tgt, rel, "structural", "deterministic", 1.0,
                              [ev.id], "sql", SQL_VERSION, file_path=rel_path, start_line=line,
                              attributes=attrs or {}))

    def table(name: str, a: int, b: int, attrs=None) -> str:
        tid = table_id(repo_id, name)
        node(tid, "table", name, name, a, b, dict(attrs or {}))
        edge(fid, "contains", tid, a)
        return tid

    def column(tname: str, tid: str, col: str, ctype: str, line: int, extra=None) -> str:
        cid = column_id(repo_id, tname, col)
        node(cid, "column", col, f"{tname}.{col}", line, line, dict({"type": ctype, "table": tname}, **(extra or {})))
        edge(tid, "contains", cid, line)
        return cid

    pending: dict = {}      # table id -> {"foreign_keys": [...], "view_sources": [...]}

    def fk_ref(src_tid: str, src_tname: str, cols: list, ref_name: str, ref_cols: list, line: int):
        # edges are minted by graph/sql_link.py once the referenced table is
        # known to exist (it may live in another migration, or not at all)
        pending.setdefault(src_tid, {}).setdefault("foreign_keys", []).append(
            {"columns": cols, "table": ref_name, "ref_columns": ref_cols, "line": line})

    for off, stmt in split_statements(clean):
        lead = len(stmt) - len(stmt.lstrip())
        s_line = line_of(off + lead)
        e_line = line_of(off + len(stmt.rstrip()))
        m = _CREATE_TABLE.match(stmt)
        if m:
            tname = _short(m.group(1))
            tid = table(tname, s_line, e_line)
            body_at = m.end() - 1
            body = _paren_body(stmt, body_at)
            for ioff, item in _top_level_items(body):
                it = item.strip()
                if not it:
                    continue
                iline = line_of(off + body_at + 1 + ioff + (len(item) - len(item.lstrip())))
                if _CONSTRAINT_HEAD.match(it):
                    fk = _FK.search(it)
                    if fk:
                        fk_ref(tid, tname, _cols(fk.group(1)), _short(fk.group(2)), _cols(fk.group(3) or ""), iline)
                    continue
                cm = re.match(rf"({_IDENT})\s+(.*)$", it, re.S)
                if not cm:
                    continue
                cname, rest = _unquote(cm.group(1)), cm.group(2)
                if cname.lower() in ("like",):
                    continue
                ctype = re.split(r"\s+", rest.strip(), 1)[0] if rest.strip() else ""
                extra = {}
                if re.search(r"\bPRIMARY\s+KEY\b", rest, re.I):
                    extra["primary_key"] = True
                if re.search(r"\bNOT\s+NULL\b", rest, re.I):
                    extra["not_null"] = True
                column(tname, tid, cname, ctype, iline, extra)
                ifk = _INLINE_FK.search(rest)
                if ifk:
                    fk_ref(tid, tname, [cname], _short(ifk.group(1)), _cols(ifk.group(2) or ""), iline)
            continue
        m = _CREATE_VIEW.match(stmt)
        if m:
            vname = _short(m.group(1))
            vid = table(vname, s_line, e_line, {"view": True})
            srcs = sorted({_short(r.group(1)) for r in _TABLE_REF.finditer(m.group(2))
                           if _short(r.group(1)) and _short(r.group(1)).lower() not in _SQL_KEYWORDS})
            if srcs:
                pending.setdefault(vid, {})["view_sources"] = srcs
            continue
        m = _ALTER.match(stmt)
        if m:
            tname = _short(m.group(1))
            tid = table(tname, s_line, e_line, {"altered": True})
            for ioff, item in _top_level_items(m.group(2)):
                it = item.strip()
                iline = line_of(off + m.start(2) + ioff + (len(item) - len(item.lstrip())))
                am = re.match(rf"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?({_IDENT})\s+(\S+)(.*)$", it, re.I | re.S)
                if am and am.group(1).upper() not in ("CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "INDEX", "KEY"):
                    column(tname, tid, _unquote(am.group(1)), am.group(2), iline)
                    ifk = _INLINE_FK.search(am.group(3))
                    if ifk:
                        fk_ref(tid, tname, [_unquote(am.group(1))], _short(ifk.group(1)), _cols(ifk.group(2) or ""), iline)
                    continue
                fk = _FK.search(it)
                if fk and re.match(r"ADD\b", it, re.I):
                    fk_ref(tid, tname, _cols(fk.group(1)), _short(fk.group(2)), _cols(fk.group(3) or ""), iline)
                    continue
                rm = re.match(rf"RENAME\s+COLUMN\s+({_IDENT})\s+TO\s+({_IDENT})", it, re.I)
                if rm:
                    old, new = _unquote(rm.group(1)), _unquote(rm.group(2))
                    column(tname, tid, new, "", iline, {"renamed_from": old})
                    drops.append({"column": f"{tname}.{old}", "line": iline, "renamed_to": new})
                    continue
                cm = re.match(rf"CHANGE\s+(?:COLUMN\s+)?({_IDENT})\s+({_IDENT})\s+(\S+)", it, re.I)
                if cm:                                   # MySQL CHANGE old new TYPE
                    old, new = _unquote(cm.group(1)), _unquote(cm.group(2))
                    column(tname, tid, new, cm.group(3), iline, {"renamed_from": old} if old.lower() != new.lower() else {})
                    if old.lower() != new.lower():
                        drops.append({"column": f"{tname}.{old}", "line": iline, "renamed_to": new})
                    continue
                dm = re.match(rf"DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?({_IDENT})", it, re.I)
                if dm and dm.group(1).upper() not in ("CONSTRAINT", "PRIMARY", "FOREIGN", "INDEX", "KEY"):
                    drops.append({"column": f"{tname}.{_unquote(dm.group(1))}", "line": iline})
                    continue
                rt = re.match(rf"RENAME\s+TO\s+({_QNAME})", it, re.I)
                if rt:
                    new = _short(rt.group(1))
                    table(new, iline, iline, {"renamed_from": tname})
                    drops.append({"table": tname, "line": iline, "renamed_to": new})
            continue
        m = _DROP_TABLE.match(stmt)
        if m:
            for nm in re.split(r"\s*,\s*", m.group(1)):
                drops.append({"table": _short(nm), "line": s_line})
    for n in res.nodes:
        if n.id in pending:
            n.attributes = dict(n.attributes or {}, **pending[n.id])
    if drops:
        file_node.attributes = dict(file_node.attributes, drops=drops)
    return res
