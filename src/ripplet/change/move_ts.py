"""move_symbol for TypeScript / JavaScript (ES modules): same promise as the Python mover, read from tree-sitter syntax trees.

The declaration (with its leading comments, `export` and overloads) leaves the old module and lands at the end of the
new one with the imports it needs; `import {name} from "./old"` and `export {name} from "./old"` elsewhere name the new
module (relative specifiers keep their `.js`/`.ts`/no-extension style); imports of the old module that nothing uses
any more are dropped (`noUnusedLocals`); the old module re-exports the name when a barrel (`export *`), a namespace
import or a `require`/`import()` could still reach it. Files are all computed first and written together, or nothing.
Refusals: the symbol needs a name defined in the old module (unless with_dependencies), CommonJS modules, default
exports, declarations sharing one `const a = 1, b = 2`, a name the target already has.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

TS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_SKIP = {"node_modules", ".git", "dist", "build", "coverage", ".next", ".turbo", "out"}
_DECLS = {"function_declaration": "function", "generator_function_declaration": "function",
          "class_declaration": "class", "abstract_class_declaration": "class",
          "interface_declaration": "type", "type_alias_declaration": "type", "enum_declaration": "enum",
          "function_signature": "function"}
_NAMES = {"identifier", "type_identifier", "shorthand_property_identifier"}
_parsers: dict = {}


def _parser(path: Path):
    key = "tsx" if path.suffix in (".tsx", ".jsx") else "ts"
    if key not in _parsers:
        import tree_sitter as ts
        import tree_sitter_typescript as t
        _parsers[key] = ts.Parser(ts.Language(t.language_tsx() if key == "tsx" else t.language_typescript()))
    return _parsers[key]


def _parse(path: Path):
    src = path.read_bytes()
    return src, _parser(path).parse(src).root_node


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _txt(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="surrogateescape")


def _eol(src: bytes) -> str:
    return "\r\n" if b"\r\n" in src else "\n"


def _free(n, src: bytes) -> set:
    return {_txt(src, x) for x in _walk(n) if x.type in _NAMES}


def _spec_of(stmt, src: bytes):
    s = stmt.child_by_field_name("source")
    if s is None:
        return None, None
    frag = next((c for c in s.children if c.type == "string_fragment"), None)
    return (_txt(src, frag) if frag is not None else ""), s


def _is_type_stmt(stmt, src: bytes) -> bool:
    return any(c.type == "type" for c in stmt.children[:3])


def _resolves_to(importer: Path, spec: str, target: Path) -> bool:
    if not spec.startswith("."):
        return False
    base = os.path.normpath(os.path.join(str(importer.parent), spec))
    cands = [base]
    m = re.match(r"(.*)\.(m?js|jsx|cjs)$", base)
    if m:
        cands += [m.group(1) + e for e in (".ts", ".tsx", ".mts", ".cts")]
    cands += [base + e for e in (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".mts")]
    cands += [os.path.join(base, "index" + e) for e in (".ts", ".tsx", ".js", ".jsx", ".mjs")]
    tgt = os.path.normcase(str(target))
    return any(os.path.normcase(c) == tgt for c in cands)


def _respell(here: Path, target: Path, old_spec: str) -> str:
    """Relative specifier from `here` to `target`, spelled like `old_spec` (extension and /index conventions)."""
    base = target.with_suffix("")
    rel = os.path.relpath(str(base), str(here.parent)).replace("\\", "/")
    tail = old_spec.rstrip("/").split("/")[-1]
    if target.stem == "index" and tail != "index" and not re.search(r"\.[mc]?[jt]sx?$", tail):
        rel = rel[:-len("/index")] if rel.endswith("/index") else "."
    elif re.search(r"\.(m?js|jsx|cjs)$", old_spec):
        rel += ".mjs" if target.suffix == ".mts" else ".js"
    elif re.search(r"\.[mc]?tsx?$", old_spec):
        rel += target.suffix
    if not rel.startswith("."):
        rel = "./" + rel
    return rel


def _carry_spec(a: Path, b: Path, spec: str) -> str:
    if not spec.startswith("."):
        return spec
    base = os.path.normpath(os.path.join(str(a.parent), spec))
    rel = os.path.relpath(base, str(b.parent)).replace("\\", "/")
    if spec.endswith("/") and not rel.endswith("/"):
        rel += "/"
    return rel if rel.startswith(".") else "./" + rel


def _style(root, src: bytes):
    """(quote, semicolon, example relative specifier) used by a file's own imports."""
    quote, semi, ex = '"', ";", ""
    first = True
    for st in root.children:
        if st.type == "import_statement":
            spec, s = _spec_of(st, src)
            if first and s is not None:
                quote = chr(src[s.start_byte])
                semi = ";" if src[st.end_byte - 1:st.end_byte] == b";" else ""
                first = False
            if spec and spec.startswith(".") and not ex:
                ex = spec
    return quote, semi, ex


def _unreadable(root, src: bytes, moved: set):
    """Why the top-level structure of a file can't be trusted, or None. The grammar chokes on a few valid forms
    (e.g. a generic call signature inside an interface); that is harmless while the damage stays inside a
    statement we neither move nor touch, but not when a top-level node is an error, or an import / moved node."""
    if not root.has_error:
        return None
    for c in root.children:
        if c.type == "ERROR":
            return f"syntax error at line {c.start_point[0] + 1}"
        if c.has_error and (c.type == "import_statement" or c.start_byte in moved):
            return f"syntax error in the {'import' if c.type == 'import_statement' else 'moved declaration'} at line {c.start_point[0] + 1}"
    return None


def _dir_example(path: Path) -> str:
    """A relative specifier used by a neighbour of `path`, to copy its extension style."""
    for q in sorted(path.parent.iterdir())[:40]:
        if q.suffix in TS_EXTS and q != path and q.is_file():
            try:
                root = _parse(q)[1]
                src = q.read_bytes()
            except Exception:
                continue
            ex = _style(root, src)[2]
            if ex:
                return ex
    return ""


def _bindings(stmt, src: bytes) -> list:
    """(kind, imported, local, node) of an import statement: kind default | ns | named."""
    out = []
    clause = next((c for c in stmt.children if c.type == "import_clause"), None)
    if clause is None:
        return out
    for c in clause.children:
        if c.type == "identifier":
            out.append(("default", "default", _txt(src, c), c))
        elif c.type == "namespace_import":
            ids = [x for x in c.children if x.type == "identifier"]
            if ids:
                out.append(("ns", "*", _txt(src, ids[-1]), c))
        elif c.type == "named_imports":
            for sp in c.children:
                if sp.type == "import_specifier":
                    nm = sp.child_by_field_name("name")
                    al = sp.child_by_field_name("alias")
                    imported = _txt(src, nm)
                    out.append(("named", imported, _txt(src, al) if al is not None else imported, sp))
    return out


def _line_range(src: bytes, a: int, b: int):
    """Widen [a, b) to whole lines when only whitespace shares them."""
    s = src.rfind(b"\n", 0, a) + 1
    if src[s:a].strip():
        s = a
    e = b
    while e < len(src) and src[e:e + 1] in (b" ", b"\t"):
        e += 1
    if e < len(src) and src[e:e + 1] == b"\r":
        e += 1
    if e < len(src) and src[e:e + 1] == b"\n":
        e += 1
    elif src[b:e].strip():
        e = b
    return (s, e) if s == a or not src[s:a].strip() else (a, e)


def _runs(items: list, gone: list) -> list:
    """Byte ranges to cut so that exactly the `gone` items leave a comma separated list."""
    cuts, i, n = [], 0, len(items)
    while i < n:
        if not gone[i]:
            i += 1
            continue
        j = i
        while j < n and gone[j]:
            j += 1
        if j < n:
            cuts.append((items[i].start_byte, items[j].start_byte))
        elif i > 0:
            cuts.append((items[i - 1].end_byte, items[j - 1].end_byte))
        else:
            cuts.append((items[i].start_byte, items[j - 1].end_byte))
        i = j
    return cuts


def _strip(stmt, src: bytes, drop):
    """Remove bindings of an import / export-from statement for which drop(kind, imported, local) is true.
    Returns (edits, whole_gone, removed_specs_text)."""
    if stmt.type == "import_statement":
        clause = next((c for c in stmt.children if c.type == "import_clause"), None)
        if clause is None:
            return [], False, []
        parts = [c for c in clause.children if c.type in ("identifier", "namespace_import", "named_imports")]
    else:
        clause = next((c for c in stmt.children if c.type == "export_clause"), None)
        if clause is None:
            return [], False, []
        parts = [clause]
    removed = []
    edits = []
    part_gone = []
    for p in parts:
        if p.type == "identifier":
            g = bool(drop("default", "default", _txt(src, p)))
            if g:
                removed.append(_txt(src, p))
            part_gone.append(g)
        elif p.type == "namespace_import":
            ids = [x for x in p.children if x.type == "identifier"]
            g = bool(ids) and bool(drop("ns", "*", _txt(src, ids[-1])))
            if g:
                removed.append(_txt(src, p))
            part_gone.append(g)
        else:
            specs = [s for s in p.children if s.type in ("import_specifier", "export_specifier")]
            gone = []
            for s in specs:
                nm = s.child_by_field_name("name")
                al = s.child_by_field_name("alias")
                imp = _txt(src, nm)
                g = bool(drop("named", imp, _txt(src, al) if al is not None else imp))
                gone.append(g)
                if g:
                    removed.append(_txt(src, s))
            if specs and all(gone):
                part_gone.append(True)
            else:
                part_gone.append(False)
                edits += _runs(specs, gone)
    if not removed:
        return [], False, []
    if all(part_gone):
        return [], True, removed
    # whole parts that go while others stay (default / namespace / braces)
    if any(part_gone):
        edits += _runs(parts, part_gone)
    return [(a, b, b"") for a, b in edits], False, removed


def _apply(src: bytes, edits: list) -> bytes:
    out = src
    for a, b, rep in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out = out[:a] + rep + out[b:]
    return out


def _top(root, src: bytes) -> dict:
    """name -> {nodes, exported, default, kind, multi}"""
    top: dict = {}
    for st in root.children:
        decl, exported, default = st, False, False
        if st.type == "export_statement":
            decl = st.child_by_field_name("declaration")
            exported = True
            default = any(c.type == "default" for c in st.children)
            if decl is None:
                continue
        kind = _DECLS.get(decl.type)
        names = []
        if kind:
            nm = decl.child_by_field_name("name")
            if nm is not None:
                names = [(_txt(src, nm), kind, False)]
        elif decl.type in ("lexical_declaration", "variable_declaration"):
            ds = [c for c in decl.children if c.type == "variable_declarator"]
            for d in ds:
                nm = d.child_by_field_name("name")
                if nm is not None and nm.type == "identifier":
                    names.append((_txt(src, nm), "const", len(ds) > 1))
        for nm, kd, multi in names:
            e = top.setdefault(nm, {"nodes": [], "exported": False, "default": False, "kind": kd, "multi": False})
            e["nodes"].append(st)
            e["exported"] = e["exported"] or exported
            e["default"] = e["default"] or default
            e["multi"] = e["multi"] or multi
            if decl.type != "function_signature":
                e["kind"] = kd
    return top


def _span(root, src: bytes, st):
    """Byte range of a top-level statement with the comments directly above it, widened to whole lines."""
    start = st.start_byte
    prev = st.prev_sibling
    while prev is not None and prev.type == "comment":
        gap = src[prev.end_byte:start]
        if gap.count(b"\n") > 1:
            break
        before = prev.prev_sibling
        if before is not None and before.end_point[0] == prev.start_point[0] and before.type != "comment":
            break
        start = prev.start_byte
        prev = prev.prev_sibling
    s = src.rfind(b"\n", 0, start) + 1
    if src[s:start].strip():
        s = start
    e = st.end_byte
    k = e
    while k < len(src) and src[k:k + 1] in (b" ", b"\t", b"\r"):
        k += 1
    if k >= len(src) or src[k:k + 1] == b"\n":
        e = k + 1 if k < len(src) else k
    return s, e


def _stmt_text(spec: str, names: list, quote: str, semi: str, kind_kw: str) -> str:
    return f"{kind_kw} {{ {', '.join(names)} }} from {quote}{spec}{quote}{semi}"


def move(store, cfg, symbol: str, to_file: str, apply: bool, with_dependencies: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    wanted = [s.strip() for s in str(symbol).split(",") if s.strip()]
    seeds, rel_a = [], None
    for sym in wanted:
        node, _o = resolve(cx, sym)
        if node is None:                       # module-level constants are 'variable' nodes, which resolve() skips
            where, _s, nm0 = sym.rpartition("::")
            rows = [r for r in cx.execute("SELECT * FROM nodes WHERE type='variable' AND label=?", (nm0.split(".")[-1],)).fetchall()
                    if (r["file_path"] or "").lower().endswith(TS_EXTS)
                    and (not where or (r["file_path"] or "").replace("\\", "/").endswith(where.replace("\\", "/")))]
            files = sorted({r["file_path"] for r in rows})
            if len(files) > 1:
                return f"move_symbol: {sym!r} is a constant in several files ({', '.join(files[:4])}); name it as path::name or move it by its file."
            node = rows[0] if rows else None
        if node is None:
            return f"move_symbol: no code symbol named {sym!r} in the index. Use search_symbols to find the exact name."
        f = (node["file_path"] or "").replace("\\", "/")
        if not f.endswith(TS_EXTS):
            return "move_symbol: the symbols to move together must be in the same file."
        if rel_a not in (None, f):
            return "move_symbol: the symbols to move together must be in the same file."
        rel_a = f
        seeds.append(node["label"])
    path_a = file_of([str(r) for r in roots], rel_a)
    if path_a is None:
        return f"move_symbol: {rel_a} is not on disk (index out of date?)."
    path_a = path_a.resolve()
    root = next((r.resolve() for r in roots if str(path_a).startswith(str(r.resolve()))), roots[0].resolve())
    path_b = Path(to_file)
    if not path_b.is_absolute():
        path_b = root / to_file
    path_b = path_b.resolve()
    if path_b.suffix not in TS_EXTS:
        return "move_symbol: to_file must be a TypeScript or JavaScript file (.ts, .tsx, .js, ...)."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."

    src_a, tree_a = _parse(path_a)
    if re.search(rb"(?m)\bmodule\.exports\b|^\s*(?:const|let|var)\s+[^\n=]*=\s*require\(|^\s*exports\.", src_a):
        return f"move_symbol: {rel_a} is a CommonJS module (require / module.exports); only ES modules are supported. Nothing was written."
    top = _top(tree_a, src_a)
    group = []
    for nm in seeds:
        e = top.get(nm)
        if e is None:
            return (f"move_symbol: {nm} is not a top-level function, class, type, enum or const of {rel_a} "
                    f"(methods and nested definitions can't be moved on their own); nothing was written.")
        if e["default"]:
            return f"move_symbol: {nm} is the default export of {rel_a}; nothing was written."
        if e["multi"]:
            return f"move_symbol: {nm} is declared together with other names in one statement; split it first. Nothing was written."
        if sum(1 for n in e["nodes"] if n.type != "export_statement" or True) and \
                len({id(n) for n in e["nodes"]}) > 1 and e["kind"] not in ("function",):
            return f"move_symbol: {nm} is declared more than once in {rel_a} (merged declarations); nothing was written."
        if nm not in group:
            group.append(nm)

    imports_a = {}
    for st in tree_a.children:
        if st.type == "import_statement":
            for kind, imp, loc, nd in _bindings(st, src_a):
                imports_a[loc] = (st, kind, imp, nd)
    added_deps = []
    while True:
        blockers, grow = [], []
        for nm in group:
            fr = set()
            for st in top[nm]["nodes"]:
                fr |= _free(st, src_a)
            for f in sorted(fr - {nm}):
                if f in group or f in imports_a or f not in top:
                    continue
                if with_dependencies and not top[f]["default"] and not top[f]["multi"]:
                    grow.append(f)
                elif f not in blockers:
                    blockers.append(f)
        grow = [g for g in dict.fromkeys(grow) if g not in group]
        if not grow:
            break
        group += grow
        added_deps += grow
    name = ", ".join(group)
    if blockers:
        hint = "" if with_dependencies else " Pass with_dependencies=true to move what it needs along, or move it first."
        return (f"move_symbol: {name} uses {', '.join(blockers)} defined in {rel_a}; moving it would make the two "
                f"modules import each other.{hint} nothing was written.")

    # statements of the group, in source order
    stmts = []
    for nm in group:
        for st in top[nm]["nodes"]:
            if not any(st.start_byte == s.start_byte for s in stmts):
                stmts.append(st)
    stmts.sort(key=lambda s: s.start_byte)
    moved_ids = {s.start_byte for s in stmts}
    eol_a = _eol(src_a)

    # which names the moved code needs from the old module's imports
    need = {}
    for st in stmts:
        for f in _free(st, src_a):
            if f in imports_a and f not in group:
                need[f] = imports_a[f]
    for loc, (ist, kind, imp, nd) in need.items():
        if ist.type == "import_statement" and any(c.type == "import_require_clause" for c in ist.children):
            return f"move_symbol: {name} needs {loc}, imported with `import = require`; nothing was written."

    # remaining code of the old module: what it still uses
    bad = _unreadable(tree_a, src_a, moved_ids)
    if bad:
        return f"move_symbol: the TypeScript parser can't read {rel_a} reliably ({bad}); nothing was written."
    rest_nodes = [s for s in tree_a.children if s.type != "import_statement" and s.start_byte not in moved_ids]
    rest_used = set()
    for s in rest_nodes:
        if s.type == "export_statement" and s.child_by_field_name("source") is not None:
            continue                            # `export { a } from "./x"` names no local binding
        rest_used |= _free(s, src_a)
    for s in rest_nodes:
        for x in _walk(s):
            tgt = x.child_by_field_name("left") if x.type in ("assignment_expression", "augmented_assignment_expression") \
                else x.child_by_field_name("argument") if x.type == "update_expression" else None
            if tgt is not None and tgt.type == "identifier" and _txt(src_a, tgt) in group:
                return (f"move_symbol: {rel_a} assigns to {_txt(src_a, tgt)} (line {x.start_point[0] + 1}), which would become "
                        f"an imported binding; an importing module can't assign to it. Nothing was written.")
    uses_in_a = [n for n in group if n in rest_used]
    # code that runs at import time and calls a moved name is left alone only if it still resolves
    exported_group = [n for n in group if top[n]["exported"]]

    # target module
    exists_b = path_b.is_file()
    src_b, tree_b = _parse(path_b) if exists_b else (b"", None)
    bad = _unreadable(tree_b, src_b, set()) if exists_b else None
    if bad:
        return f"move_symbol: the TypeScript parser can't read {path_b.name} reliably ({bad}); nothing was written."
    eol_b = _eol(src_b) if exists_b else eol_a
    top_b = _top(tree_b, src_b) if exists_b else {}
    imports_b = {}
    if exists_b:
        for st in tree_b.children:
            if st.type == "import_statement":
                for kind, imp, loc, nd in _bindings(st, src_b):
                    imports_b[loc] = (st, kind, imp, nd)
    clash = [n for n in group if n in top_b or n in imports_b]
    if clash:
        return f"move_symbol: {path_b.name} already defines or imports {', '.join(clash)}; nothing was written."
    qa, sa, ex_a = _style(tree_a, src_a)
    if not ex_a:
        ex_a = _dir_example(path_a)
    qb, sb, ex_b = _style(tree_b, src_b) if exists_b else (qa, sa, ex_a)

    # imports to add to the new module, grouped per source statement
    by_stmt: dict = {}
    for loc, (ist, kind, imp, nd) in need.items():
        by_stmt.setdefault(ist.start_byte, (ist, []))[1].append((loc, kind, imp, nd))
    new_import_texts, merges = [], []
    carried_names = []
    for _k, (ist, items) in sorted(by_stmt.items()):
        spec, _s = _spec_of(ist, src_a)
        new_spec = _carry_spec(path_a, path_b, spec)
        type_only = _is_type_stmt(ist, src_a)
        todo = []
        for loc, kind, imp, nd in items:
            if loc in imports_b:
                ob = imports_b[loc]
                ob_spec, _ = _spec_of(ob[0], src_b)
                if ob_spec != new_spec:
                    return (f"move_symbol: {path_b.name} already imports {loc} from {ob_spec!r}, but {name} needs it from "
                            f"{new_spec!r}; nothing was written.")
                continue
            if loc in top_b:
                return f"move_symbol: {path_b.name} defines {loc}, which {name} needs from an import; nothing was written."
            todo.append((loc, kind, imp, nd))
        if not todo:
            continue
        carried_names += [t[0] for t in todo]
        kw = "import type" if type_only else "import"
        default = next((t for t in todo if t[1] == "default"), None)
        ns = next((t for t in todo if t[1] == "ns"), None)
        named = [t for t in todo if t[1] == "named"]
        # merge into an existing statement of the new module with the same source
        host = None
        if named and not default and not ns:
            for st in (tree_b.children if exists_b else []):
                if st.type == "import_statement" and _spec_of(st, src_b)[0] == new_spec \
                        and _is_type_stmt(st, src_b) == type_only:
                    ni = next((x for c in st.children if c.type == "import_clause" for x in c.children
                               if x.type == "named_imports"), None)
                    if ni is not None:
                        host = ni
                        break
        if host is not None:
            specs = [x for x in host.children if x.type == "import_specifier"]
            merges.append((specs[-1].end_byte if specs else host.start_byte + 1,
                           (", " if specs else "") + ", ".join(_txt(src_a, t[3]) for t in named)))
            continue
        head = []
        if default:
            head.append(default[0])
        if ns:
            head.append("* as " + ns[0])
        if named:
            head.append("{ " + ", ".join(_txt(src_a, t[3]) for t in named) + " }")
        new_import_texts.append(f"{kw} {', '.join(head)} from {qb}{new_spec}{qb}{sb}")

    # the other modules
    skip_a_b = {os.path.normcase(str(path_a)), os.path.normcase(str(path_b))}
    new_files: dict = {}
    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")
    rewritten, dynamic, unresolved, reach = [], [], [], False
    stem = path_a.stem if path_a.stem != "index" else path_a.parent.name
    for p in sorted(root.rglob("*")):
        if p.suffix not in TS_EXTS or any(x in _SKIP for x in p.relative_to(root).parts) \
                or os.path.normcase(str(p.resolve())) in skip_a_b:
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        txt = raw.decode("utf-8", errors="ignore")
        if "declare module" in txt:
            hit = [g for g in group if top[g]["kind"] in ("type", "class", "enum") and re.search(r"\b" + re.escape(g) + r"\b", txt)]
            if hit:
                return (f"move_symbol: {rel(p)} has a `declare module` block that may augment {', '.join(hit)} by its module path; "
                        f"moving it would change what that merges into. Nothing was written.")
        if not (stem in txt):
            continue
        try:
            sr, tr = _parse(p)
        except Exception:
            continue
        edits = []
        n_here = 0
        for st in tr.children:
            if st.type not in ("import_statement", "export_statement") or st.has_error:
                continue
            spec, sn = _spec_of(st, sr)
            if spec is None:
                continue
            if not _resolves_to(p, spec, path_a):
                if not spec.startswith(".") and (spec.endswith("/" + stem) or spec == stem) \
                        and any(g in txt for g in group):
                    unresolved.append((p, st.start_point[0] + 1))
                continue
            is_export = st.type == "export_statement"
            if is_export and any(c.type == "*" for c in st.children) or \
                    (is_export and any(c.type == "namespace_export" for c in st.children)):
                reach = True
                continue
            if not is_export:
                bs = _bindings(st, sr)
                if any(k == "ns" for k, _i, _l, _n in bs):
                    for k, _i, loc, _n in bs:
                        if k == "ns" and re.search(r"\b" + re.escape(loc) + r"\s*\.\s*(" + "|".join(map(re.escape, group)) + r")\b", txt):
                            reach = True
                            dynamic.append((p, f"{loc}.{next(g for g in group if re.search(re.escape(loc) + r'\s*\.\s*' + re.escape(g) + r'\b', txt))}"))
            drop = lambda kind, imp, loc: kind == "named" and imp in group
            ed, whole, removed = _strip(st, sr, drop)
            if not removed:
                continue
            q = chr(sr[sn.start_byte])
            semi = ";" if sr[st.end_byte - 1:st.end_byte] == b";" else ""
            new_spec = _respell(p, path_b, spec)
            kw = ("export" if is_export else "import") + (" type" if _is_type_stmt(st, sr) else "")
            line = f"{kw} {{ {', '.join(removed)} }} from {q}{new_spec}{q}{semi}"
            eol = _eol(sr)
            if whole:
                edits.append((st.start_byte, st.end_byte, line.encode()))
            else:
                edits += ed
                edits.append((st.end_byte, st.end_byte, (eol + line).encode()))
            n_here += 1
        # dynamic uses: require("./a") / import("./a") / jest.mock("./a")
        for x in _walk(tr):
            if x.type == "string" and x.parent is not None and x.parent.type == "arguments":
                frag = next((c for c in x.children if c.type == "string_fragment"), None)
                if frag is not None and _resolves_to(p, _txt(sr, frag), path_a):
                    dynamic.append((p, "a require / import() / mock call"))
                    reach = True
        if edits:
            new_files[p.resolve()] = _apply(sr, edits)
            rewritten.append((p, n_here))

    # the old module
    a_edits = []
    exported_moved = [n for n in group if top[n]["exported"]]
    keep_reexport = [n for n in exported_moved if reach]
    for st in stmts:
        s, e = _span(tree_a, src_a, st)
        a_edits.append((s, e, b""))
    # imports that only the moved code used
    used_by_moved = set(need)
    gone_loc = {loc for loc in used_by_moved if loc not in rest_used and loc not in group}
    for st in tree_a.children:
        if st.type != "import_statement":
            continue
        ed, whole, removed = _strip(st, src_a, lambda k, i, l: l in gone_loc)
        if whole:
            s, e = _line_range(src_a, st.start_byte, st.end_byte)
            a_edits.append((s, e, b""))
        else:
            a_edits += ed
    # re-import / re-export of the moved names
    last_imp = max((s.end_byte for s in tree_a.children if s.type == "import_statement"), default=None)
    add_a = []
    spec_ab = _respell(path_a, path_b, ex_a)
    use_vals = [n for n in uses_in_a if top[n]["kind"] in ("function", "class", "const", "enum")]
    use_types = [n for n in uses_in_a if n not in use_vals]
    if use_vals:
        add_a.append(_stmt_text(spec_ab, use_vals, qa, sa, "import"))
    if use_types:
        add_a.append(_stmt_text(spec_ab, use_types, qa, sa, "import type"))
    if keep_reexport:
        rv = [n for n in keep_reexport if top[n]["kind"] in ("function", "class", "const", "enum")]
        rt = [n for n in keep_reexport if n not in rv]
        if rv:
            add_a.append(_stmt_text(spec_ab, rv, qa, sa, "export"))
        if rt:
            add_a.append(_stmt_text(spec_ab, rt, qa, sa, "export type"))
    if add_a:
        ins = ("\n".join(add_a)).replace("\n", eol_a)
        if last_imp is not None:
            a_edits.append((last_imp, last_imp, (eol_a + ins).encode()))
        else:
            a_edits.append((0, 0, (ins + eol_a + eol_a).encode()))
    out_a = _apply(src_a, a_edits)
    out_a = re.sub(rb"(\r?\n){4,}", (eol_a * 3).encode(), out_a)
    out_a = re.sub(rb"\A(\r?\n)+", b"", out_a)
    new_files[path_a] = out_a

    # the new module
    seg_parts = []
    for st in stmts:
        s, e = _span(tree_a, src_a, st)
        t = src_a[s:e].decode("utf-8", errors="surrogateescape").rstrip("\r\n")
        nm_of = [n for n in group if any(st.start_byte == x.start_byte for x in top[n]["nodes"])]
        if st.type != "export_statement" and any(n in uses_in_a or n in keep_reexport for n in nm_of):
            indent = t[:len(t) - len(t.lstrip())]
            t = indent + "export " + t.lstrip()
        seg_parts.append(t)
    seg = ("\n\n".join(seg_parts)).replace("\r\n", "\n").replace("\n", eol_b)
    b_edits = list(merges and [(pos, pos, txt.encode()) for pos, txt in merges])
    b_imports = None
    if new_import_texts:
        b_imports = eol_b.join(new_import_texts)
    if exists_b:
        last_b = max((s.end_byte for s in tree_b.children if s.type == "import_statement"), default=None)
        if b_imports:
            if last_b is not None:
                b_edits.append((last_b, last_b, (eol_b + b_imports).encode()))
            else:
                first = tree_b.children[0] if tree_b.children else None
                at = 0
                if first is not None and first.type == "expression_statement" and _txt(src_b, first).lstrip().startswith(("'use ", '"use ')):
                    at = first.end_byte
                    b_edits.append((at, at, (eol_b + b_imports).encode()))
                else:
                    b_edits.append((0, 0, (b_imports + eol_b + eol_b).encode()))
        body = _apply(src_b, b_edits).rstrip()
        out_b = body + (eol_b * 2).encode() + seg.encode() + eol_b.encode()
    else:
        head = (b_imports + eol_b + eol_b) if b_imports else ""
        out_b = (head + seg + eol_b).encode()
    new_files[path_b] = out_b

    if apply:
        for p, data in new_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")
    verb = "moved" if apply else "would move"
    n_lines = sum(src_a[_span(tree_a, src_a, st)[0]:_span(tree_a, src_a, st)[1]].count(b"\n") for st in stmts)
    out = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
           f"{verb} {name} ({n_lines} lines) from {rel_a} to {rel(path_b)}" + ("" if exists_b else " (new file)")]
    if added_deps:
        out.append("moved along because they need it: " + ", ".join(added_deps))
    if carried_names or merges:
        out.append("imports added to the new module: " + "; ".join(new_import_texts + [m[1].lstrip(", ") for m in merges]))
    if gone_loc:
        out.append(f"{rel_a}: dropped imports nothing uses any more: " + ", ".join(sorted(gone_loc)))
    for p, n in rewritten:
        out.append(f"imports rewritten in {rel(p)}: {n}")
    if use_vals or use_types:
        out.append(f"{rel_a} now imports {', '.join(use_vals + use_types)} from the new module (it still uses them).")
    if keep_reexport:
        out.append(f"{rel_a} keeps `export {{ {', '.join(keep_reexport)} }} from` the new module because a barrel (`export *`), "
                   f"a namespace import or a require / import() call could still reach it.")
    for p, what in dynamic:
        out.append(f"  {rel(p)}: {what} still resolves through that re-export")
    if unresolved:
        out.append("NOT EDITED (a path alias or package name for the old module, I can't resolve it; change by hand):")
        out += [f"  {rel(p)}:{ln}" for p, ln in unresolved]
    if apply:
        out += ["", "Done: the definitions, their imports and the import statements are already changed. "
                    "Do NOT Read or Edit these files again to redo it.",
                "Next: check_change to confirm nothing is left, then run the related tests."]
    return "\n".join(out)
