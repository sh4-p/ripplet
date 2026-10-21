"""Database schema stated in code: migrations of the common frameworks and
Prisma schemas, as the same table/column nodes and `drops` the SQL DDL
extractor produces (so check_change's schema-break rule, sql_link and
task_context work on them unchanged).

  Laravel   Schema::create('t', fn (Blueprint $table) => $table->string('c'))
            Schema::table / dropColumn / renameColumn / Schema::drop[IfExists] / rename
  Rails     create_table :t do |t| t.string :c end, add/remove/rename_column, drop/rename_table
  Django    migrations.CreateModel / AddField / RemoveField / RenameField / DeleteModel / RenameModel
  Alembic   op.create_table('t', sa.Column('c', ..)), add/drop/alter_column, drop/rename_table
  EF Core   migrationBuilder.CreateTable / AddColumn / DropColumn / RenameColumn / DropTable / RenameTable
  Prisma    model T { c String  @map("col") }  @@map("t")

Only the forward half of a migration counts: `down()` / `downgrade()` /
`Down(...)` undo what `up` did, and reading their drops would report every
created table as dropped. Patterns are textual and conservative: a call
whose names are not string/symbol literals is skipped, never guessed.
"""
from __future__ import annotations

import re

from ...core.identifiers import edge_id, file_node_id
from ...core.models import Edge, Location, Node
from .sql import column_id, table_id

SCHEMA_CODE_VERSION = "1"

_PATH_RX = re.compile(r"(migrat|alembic/|(^|/)schema\.prisma$|\.prisma$)", re.I)


def is_schema_path(rel: str) -> bool:
    return bool(_PATH_RX.search((rel or "").replace("\\", "/")))


# the backward half of a migration, up to the next forward half or the end
_DOWN = {
    "php": (re.compile(r"function\s+down\s*\("), re.compile(r"function\s+up\s*\(")),
    "ruby": (re.compile(r"^\s*def\s+(down|self\.down)\b", re.M), re.compile(r"^\s*def\s+(up|change|self\.up)\b", re.M)),
    "python": (re.compile(r"^\s*def\s+downgrade\s*\(", re.M), re.compile(r"^\s*def\s+upgrade\s*\(", re.M)),
    "csharp": (re.compile(r"\bvoid\s+Down\s*\("), re.compile(r"\bvoid\s+Up\s*\(")),
}


def _forward_only(text: str, lang: str) -> str:
    """Blank out the down/downgrade bodies, keeping offsets (and lines) intact."""
    rx = _DOWN.get(lang)
    if not rx:
        return text
    down, up = rx
    out, pos = [], 0
    for m in down.finditer(text):
        if m.start() < pos:
            continue
        nxt = up.search(text, m.end())
        end = nxt.start() if nxt else len(text)
        out.append(text[pos:m.start()])
        out.append(re.sub(r"[^\n]", " ", text[m.start():end]))
        pos = end
    out.append(text[pos:])
    return "".join(out)


_Q = r"""['"]([A-Za-z_][\w]*)['"]"""          # a quoted identifier
_S = r""":([A-Za-z_]\w*)|['"]([A-Za-z_]\w*)['"]"""   # ruby :sym or 'str'


def _block(text: str, start: int) -> tuple[int, int]:
    """(start, end) of the bracketed body opened at or after `start`."""
    i = start
    while i < len(text) and text[i] not in "({[":
        i += 1
    if i >= len(text):
        return start, start
    depth, j = 0, i
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack = []
    while j < len(text):
        ch = text[j]
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return i, j + 1
        j += 1
    return i, len(text)


def _ruby_block(text: str, start: int) -> tuple[int, int]:
    """Body of the `do |t| ... end` block opened on the line of `start`:
    up to the first `end` indented like that line (Rails migrations are
    consistently indented; no keyword counting needed)."""
    nl = chr(10)
    ls = text.rfind(nl, 0, start) + 1
    le = text.find(nl, start)
    le = len(text) if le < 0 else le
    if not re.search(r"\bdo\b", text[start:le]):
        return start, start
    indent = len(text[ls:start]) - len(text[ls:start].lstrip())
    m = re.compile(r"^[ \t]{%d}end\b" % indent, re.M).search(text, le)
    return le, (m.start() if m else len(text))


class _Schema:
    def __init__(self):
        self.tables: dict = {}      # name -> (line, {col: (type, line)})
        self.drops: list = []

    def table(self, name: str, line: int):
        self.tables.setdefault(name, (line, {}))

    def column(self, table: str, col: str, ctype: str, line: int):
        self.table(table, line)
        self.tables[table][1].setdefault(col, (ctype, line))


_LARAVEL_SPECIAL = {"id": ["id"], "timestamps": ["created_at", "updated_at"],
                    "timestampsTz": ["created_at", "updated_at"], "softDeletes": ["deleted_at"],
                    "softDeletesTz": ["deleted_at"], "rememberToken": ["remember_token"]}
_LARAVEL_SKIP = {"index", "unique", "primary", "foreign", "dropForeign", "dropIndex", "dropUnique",
                 "dropPrimary", "engine", "charset", "collation", "comment", "temporary", "spatialIndex",
                 "fullText", "dropConstrainedForeignId", "dropTimestamps", "dropSoftDeletes"}


def _laravel(text: str, sc: _Schema, line_of):
    for m in re.finditer(r"Schema::(create|table)\s*\(\s*" + _Q, text):
        tname, line = m.group(2), line_of(m.start())
        sc.table(tname, line)
        a, b = _block(text, m.start())        # the whole Schema::create(...) call
        body = text[a:b]
        for c in re.finditer(r"\$\w+->(\w+)\s*\(([^)]*)\)", body):
            meth, args = c.group(1), c.group(2)
            cl = line_of(a + c.start())
            names = re.findall(_Q, args)
            if meth == "dropColumn":
                for n in names:
                    sc.drops.append({"column": f"{tname}.{n}", "line": cl})
            elif meth == "renameColumn" and len(names) >= 2:
                sc.drops.append({"column": f"{tname}.{names[0]}", "line": cl, "renamed_to": names[1]})
                sc.column(tname, names[1], "", cl)
            elif meth in _LARAVEL_SPECIAL and not names:
                for n in _LARAVEL_SPECIAL[meth]:
                    sc.column(tname, n, meth, cl)
            elif meth in ("morphs", "nullableMorphs", "uuidMorphs") and names:
                sc.column(tname, names[0] + "_id", meth, cl)
                sc.column(tname, names[0] + "_type", meth, cl)
            elif meth not in _LARAVEL_SKIP and not meth.startswith("drop") and names:
                sc.column(tname, names[0], meth, cl)
    for m in re.finditer(r"Schema::(drop|dropIfExists)\s*\(\s*" + _Q, text):
        sc.drops.append({"table": m.group(2), "line": line_of(m.start())})
    for m in re.finditer(r"Schema::rename\s*\(\s*" + _Q + r"\s*,\s*" + _Q, text):
        sc.drops.append({"table": m.group(1), "line": line_of(m.start()), "renamed_to": m.group(2)})
        sc.table(m.group(2), line_of(m.start()))


def _sym(m, i):
    return m.group(i) or m.group(i + 1)


_RAILS_SPECIAL = {"timestamps": ["created_at", "updated_at"]}


def _rails(text: str, sc: _Schema, line_of):
    for m in re.finditer(r"\bcreate_table\s*\(?\s*(?:" + _S + r")", text):
        tname, line = _sym(m, 1), line_of(m.start())
        sc.table(tname, line)
        if not re.search(r"id:\s*false", text[m.end():text.find("\n", m.end())]):
            sc.column(tname, "id", "primary_key", line)
        a, b = _ruby_block(text, m.end())
        for c in re.finditer(r"^\s*\w+\.(\w+)\s*\(?\s*(?:" + _S + r")?", text[a:b], re.M):
            meth, col = c.group(1), _sym(c, 2)
            cl = line_of(a + c.start())
            if meth in _RAILS_SPECIAL:
                for n in _RAILS_SPECIAL[meth]:
                    sc.column(tname, n, meth, cl)
            elif meth in ("references", "belongs_to") and col:
                sc.column(tname, col + "_id", meth, cl)
            elif col and meth not in ("index", "remove", "rename", "change", "foreign_key"):
                sc.column(tname, col, meth, cl)
    two = r"\s*\(?\s*(?:" + _S + r")\s*,\s*(?:" + _S + r")"
    for m in re.finditer(r"\badd_column" + two + r"(?:\s*,\s*:(\w+))?", text):
        sc.column(_sym(m, 1), _sym(m, 3), m.group(5) or "", line_of(m.start()))
    for m in re.finditer(r"\badd_reference" + two, text):
        sc.column(_sym(m, 1), _sym(m, 3) + "_id", "references", line_of(m.start()))
    for m in re.finditer(r"\bremove_column" + two, text):
        sc.drops.append({"column": f"{_sym(m, 1)}.{_sym(m, 3)}", "line": line_of(m.start())})
    for m in re.finditer(r"\brename_column" + two + r"\s*,\s*(?:" + _S + r")", text):
        t, old, new = _sym(m, 1), _sym(m, 3), _sym(m, 5)
        sc.drops.append({"column": f"{t}.{old}", "line": line_of(m.start()), "renamed_to": new})
        sc.column(t, new, "", line_of(m.start()))
    for m in re.finditer(r"\bdrop_table\s*\(?\s*(?:" + _S + r")", text):
        sc.drops.append({"table": _sym(m, 1), "line": line_of(m.start())})
    for m in re.finditer(r"\brename_table" + two, text):
        sc.drops.append({"table": _sym(m, 1), "line": line_of(m.start()), "renamed_to": _sym(m, 3)})
        sc.table(_sym(m, 3), line_of(m.start()))


def _kw(args: str, key: str) -> str:
    m = re.search(rf"\b{key}\s*=\s*['\"]([\w.]+)['\"]", args)
    return m.group(1) if m else ""


def _django(text: str, sc: _Schema, line_of, rel: str):
    parts = rel.replace("\\", "/").split("/")
    app = parts[parts.index("migrations") - 1] if "migrations" in parts[1:] else ""
    if not app:
        return
    tab = lambda model: f"{app}_{model.lower()}"
    for m in re.finditer(r"migrations\.(CreateModel|AddField|RemoveField|RenameField|DeleteModel|RenameModel)\s*\(",
                         text):
        op, line = m.group(1), line_of(m.start())
        a, b = _block(text, m.end() - 1)
        args = text[a:b]
        if op == "CreateModel":
            name = _kw(args, "name")
            if not name:
                continue
            db = re.search(r"['\"]db_table['\"]\s*:\s*['\"](\w+)['\"]", args)
            t = db.group(1) if db else tab(name)
            sc.column(t, "id", "AutoField", line)
            fm = re.search(r"fields\s*=\s*\[", args)
            if fm:
                fa, fb = _block(args, fm.end() - 1)
                for f in re.finditer(r"\(\s*['\"](\w+)['\"]\s*,\s*[\w.]*?(\w+Field|ForeignKey|OneToOneField)\b",
                                     args[fa:fb]):
                    col = f.group(1) + ("_id" if f.group(2) in ("ForeignKey", "OneToOneField") else "")
                    sc.column(t, col, f.group(2), line_of(a + fa + f.start()))
        elif op == "AddField":
            model, name = _kw(args, "model_name"), _kw(args, "name")
            fk = re.search(r"field\s*=\s*[\w.]*?(ForeignKey|OneToOneField)\b", args)
            if model and name:
                sc.column(tab(model), name + ("_id" if fk else ""), "", line)
        elif op == "RemoveField":
            model, name = _kw(args, "model_name"), _kw(args, "name")
            if model and name:
                sc.drops.append({"column": f"{tab(model)}.{name}", "line": line})
        elif op == "RenameField":
            model, old, new = _kw(args, "model_name"), _kw(args, "old_name"), _kw(args, "new_name")
            if model and old and new:
                sc.drops.append({"column": f"{tab(model)}.{old}", "line": line, "renamed_to": new})
                sc.column(tab(model), new, "", line)
        elif op == "DeleteModel":
            name = _kw(args, "name")
            if name:
                sc.drops.append({"table": tab(name), "line": line})
        elif op == "RenameModel":
            old, new = _kw(args, "old_name"), _kw(args, "new_name")
            if old and new:
                sc.drops.append({"table": tab(old), "line": line, "renamed_to": tab(new)})
                sc.table(tab(new), line)


def _alembic(text: str, sc: _Schema, line_of):
    for m in re.finditer(r"\bop\.create_table\s*\(\s*" + _Q, text):
        t, line = m.group(1), line_of(m.start())
        sc.table(t, line)
        a, b = _block(text, m.start() + len("op.create_table") - 1)
        for c in re.finditer(r"\bColumn\s*\(\s*" + _Q + r"\s*,?\s*(?:sa\.)?(\w+)?", text[a:b]):
            sc.column(t, c.group(1), c.group(2) or "", line_of(a + c.start()))
    for m in re.finditer(r"\bop\.add_column\s*\(\s*" + _Q + r"\s*,\s*(?:sa\.)?Column\s*\(\s*" + _Q, text):
        sc.column(m.group(1), m.group(2), "", line_of(m.start()))
    for m in re.finditer(r"\bop\.drop_column\s*\(\s*" + _Q + r"\s*,\s*" + _Q, text):
        sc.drops.append({"column": f"{m.group(1)}.{m.group(2)}", "line": line_of(m.start())})
    for m in re.finditer(r"\bop\.alter_column\s*\(\s*" + _Q + r"\s*,\s*" + _Q + r"[^)]*?new_column_name\s*=\s*"
                         + _Q, text, re.S):
        sc.drops.append({"column": f"{m.group(1)}.{m.group(2)}", "line": line_of(m.start()),
                         "renamed_to": m.group(3)})
        sc.column(m.group(1), m.group(3), "", line_of(m.start()))
    for m in re.finditer(r"\bop\.drop_table\s*\(\s*" + _Q, text):
        sc.drops.append({"table": m.group(1), "line": line_of(m.start())})
    for m in re.finditer(r"\bop\.rename_table\s*\(\s*" + _Q + r"\s*,\s*" + _Q, text):
        sc.drops.append({"table": m.group(1), "line": line_of(m.start()), "renamed_to": m.group(2)})
        sc.table(m.group(2), line_of(m.start()))


def _named_args(args: str) -> dict:
    return {k: v for k, v in re.findall(r"\b(\w+)\s*:\s*\"(\w+)\"", args)}


def _efcore(text: str, sc: _Schema, line_of):
    for m in re.finditer(r"\.(CreateTable|AddColumn|DropColumn|RenameColumn|DropTable|RenameTable)\s*(?:<[^>]*>)?\s*\(",
                         text):
        op, line = m.group(1), line_of(m.start())
        a, b = _block(text, m.end() - 1)
        args = text[a:b]
        kv = _named_args(args[:400] if op != "CreateTable" else args.split("columns:", 1)[0])
        name, table, new = kv.get("name", ""), kv.get("table", ""), kv.get("newName", "")
        if op == "CreateTable" and name:
            sc.table(name, line)
            for c in re.finditer(r"\b(\w+)\s*=\s*table\.Column<([\w?]+)>", args):
                sc.column(name, c.group(1), c.group(2), line_of(a + c.start()))
        elif op == "AddColumn" and name and table:
            sc.column(table, name, "", line)
        elif op == "DropColumn" and name and table:
            sc.drops.append({"column": f"{table}.{name}", "line": line})
        elif op == "RenameColumn" and name and table and new:
            sc.drops.append({"column": f"{table}.{name}", "line": line, "renamed_to": new})
            sc.column(table, new, "", line)
        elif op == "DropTable" and name:
            sc.drops.append({"table": name, "line": line})
        elif op == "RenameTable" and name and new:
            sc.drops.append({"table": name, "line": line, "renamed_to": new})
            sc.table(new, line)


_PRISMA_SCALARS = {"String", "Int", "BigInt", "Float", "Decimal", "Boolean", "DateTime", "Json", "Bytes"}


def _prisma(text: str, sc: _Schema, line_of):
    enums = set(re.findall(r"^\s*enum\s+(\w+)\s*\{", text, re.M))
    for m in re.finditer(r"^\s*model\s+(\w+)\s*\{", text, re.M):
        a, b = _block(text, m.end() - 1)
        body = text[a:b]
        mp = re.search(r"@@map\(\s*\"(\w+)\"\s*\)", body)
        t = mp.group(1) if mp else m.group(1)
        sc.table(t, line_of(m.start()))
        for f in re.finditer(r"^\s*(\w+)\s+(\w+)(\[\])?\??([^\n]*)$", body, re.M):
            name, typ, lst, rest = f.group(1), f.group(2), f.group(3), f.group(4)
            if name.startswith("@@") or lst or (typ not in _PRISMA_SCALARS and typ not in enums):
                continue      # relation fields hold no column; lists are relations
            col = re.search(r"@map\(\s*\"(\w+)\"\s*\)", rest)
            sc.column(t, col.group(1) if col else name, typ, line_of(a + f.start()))


def augment(res, rel_path: str, text: str, repo_id: str, project_id: str) -> bool:
    """Add the schema a migration/schema file states to its extraction
    result. Returns True when anything was found."""
    if not is_schema_path(rel_path):
        return False
    low = rel_path.lower()
    line_at = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]

    def line_of(off: int) -> int:
        lo, hi = 0, len(line_at) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_at[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    sc = _Schema()
    if low.endswith(".php") and "Schema::" in text:
        _laravel(_forward_only(text, "php"), sc, line_of)
    elif low.endswith(".rb") and re.search(r"\b(create_table|add_column|remove_column|rename_column|drop_table)\b",
                                           text):
        _rails(_forward_only(text, "ruby"), sc, line_of)
    elif low.endswith(".py") and "migrations." in text and "/migrations/" in "/" + low:
        _django(text, sc, line_of, rel_path)
    elif low.endswith(".py") and re.search(r"\bop\.(create_table|add_column|drop_column|alter_column|drop_table)",
                                           text):
        _alembic(_forward_only(text, "python"), sc, line_of)
    elif low.endswith(".cs") and "migrationBuilder" in text:
        _efcore(_forward_only(text, "csharp"), sc, line_of)
    elif low.endswith(".prisma"):
        _prisma(text, sc, line_of)
    if not sc.tables and not sc.drops:
        return False
    fid = file_node_id(repo_id, rel_path)
    ev_ids = [res.evidence[0].id] if res.evidence else []
    have = {n.id for n in res.nodes}
    lang = "sql"

    def node(nid, typ, label, qn, line, attrs):
        if nid in have:
            return
        have.add(nid)
        res.nodes.append(Node(nid, typ, label, qn, project_id, repo_id, rel_path, Location(line, 0, line, 0),
                              lang, "", attrs, list(ev_ids), schema_version=2))

    def edge(src, tgt, line):
        res.edges.append(Edge(edge_id(src, "contains", tgt, line), src, tgt, "contains", "structural",
                              "deterministic", 1.0, list(ev_ids), "schema-code", SCHEMA_CODE_VERSION,
                              file_path=rel_path, start_line=line, attributes={}))

    for tname, (tline, cols) in sc.tables.items():
        tid = table_id(repo_id, tname)
        node(tid, "table", tname, tname, tline, {"declared_by": "migration-code"})
        edge(fid, tid, tline)
        for col, (ctype, cline) in cols.items():
            cid = column_id(repo_id, tname, col)
            node(cid, "column", col, f"{tname}.{col}", cline, {"type": ctype, "table": tname})
            edge(tid, cid, cline)
    for n in res.nodes:
        if n.id == fid and n.type == "file":
            a = dict(n.attributes or {})
            if sc.drops:
                a["drops"] = sc.drops
            a["schema_code"] = SCHEMA_CODE_VERSION
            n.attributes = a
    return True
