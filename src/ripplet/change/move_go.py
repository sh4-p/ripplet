"""move_symbol for Go: move a top-level func, type (with its methods), const or var to another file.

Inside one package (same directory) that is only a change of file: the declaration with its doc comment goes, the
imports it needs go with it, and an import nothing uses any more is dropped from the old file.

To a file in another directory the symbol changes package, and the code that named it changes with it:

* `old.Name` elsewhere becomes `new.Name` (a file of the new package drops the qualifier), imports follow;
* a file of the old package that used `Name` bare now writes `new.Name` and imports the new package;
* the moved code that used identifiers of the old package writes `old.X` and imports the old package.
It refuses (writing nothing) when that cannot be done safely: an unexported name still used elsewhere, unexported
old-package identifiers used by the moved code, methods of the type in other files, an import cycle the move would
create, a dot import, cgo, `main`/`init`, a name that already exists in the target package.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_SKIP = {"node_modules", "vendor", "testdata", ".git"}
_parser = None


def _parse_bytes(src: bytes):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_go as tg
        _parser = ts.Parser(ts.Language(tg.language()))
    return _parser.parse(src).root_node


def _txt(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _go_files(root: Path) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("."))
        out += [Path(dirpath) / f for f in sorted(filenames) if f.endswith(".go")]
    return out


def _pkg_clause(src: bytes, root) -> str:
    for c in root.children:
        if c.type == "package_clause":
            return _txt(src, c.children[-1])
    return ""


def _unquote(src: bytes, lit) -> str:
    return _txt(src, lit).strip('"`')


def _imports(src: bytes, root) -> list:
    """[(spec node, decl node, alias or None, path)]"""
    out = []
    for d in root.children:
        if d.type != "import_declaration":
            continue
        for s in _walk(d):
            if s.type == "import_spec":
                path = s.child_by_field_name("path")
                nm = s.child_by_field_name("name")
                out.append((s, d, _txt(src, nm) if nm is not None else None, _unquote(src, path)))
    return out


def _is_exported(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _recv_base(src: bytes, m) -> str:
    rec = m.child_by_field_name("receiver")
    for x in _walk(rec) if rec is not None else []:
        if x.type == "type_identifier":
            return _txt(src, x)
    return ""


def _specs(decl) -> list:
    return [x for x in _walk(decl) if x.type in ("type_spec", "type_alias", "const_spec", "var_spec")]


def _spec_names(src: bytes, spec) -> list:
    if spec.type in ("type_spec", "type_alias"):
        return [_txt(src, spec.child_by_field_name("name"))]
    return [_txt(src, c) for c in spec.children if c.type == "identifier"]


def _doc_start(src: bytes, decl) -> int:
    """Start of the comment lines that sit directly above a declaration."""
    start, row, prev = decl.start_byte, decl.start_point[0], decl.prev_sibling
    while prev is not None and prev.type == "comment" and prev.end_point[0] + 1 == row:
        start, row, prev = prev.start_byte, prev.start_point[0], prev.prev_sibling
    return start


def _tops(src: bytes, root) -> dict:
    """name -> list of (kind, node, n_specs, n_names) for top-level func / type / const / var."""
    out: dict = {}
    for c in root.children:
        if c.type == "function_declaration":
            out.setdefault(_txt(src, c.child_by_field_name("name")), []).append(("func", c, 1, 1))
        elif c.type in ("type_declaration", "const_declaration", "var_declaration"):
            sp = _specs(c)
            for s in sp:
                for nm in _spec_names(src, s):
                    out.setdefault(nm, []).append((c.type.split("_")[0], c, len(sp), len(_spec_names(src, s))))
    return out


def _methods(src: bytes, root) -> list:
    return [(c, _recv_base(src, c), _txt(src, c.child_by_field_name("name"))) for c in root.children
            if c.type == "method_declaration"]


def _qualifiers(src: bytes, root, skip: list | None = None) -> set:
    """Identifiers used as a package qualifier: `q.X` selectors and `q.T` types."""
    out = set()
    for n in _walk(root):
        if skip and any(a <= n.start_byte and n.end_byte <= b for a, b in skip):
            continue
        if n.type == "selector_expression":
            op = n.child_by_field_name("operand")
            if op is not None and op.type == "identifier":
                out.add(_txt(src, op))
        elif n.type == "qualified_type":
            out.add(_txt(src, n.children[0]))
    return out


def _apply(src: bytes, edits: list) -> bytes:
    for a, b, rep in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        src = src[:a] + rep + src[b:]
    return src


_names_cache: dict = {}


def _pkg_names(modroot: Path, paths: set) -> dict:
    """Import path -> package name. `go list` knows; without it fall back to the usual naming."""
    need = sorted(p for p in paths if (str(modroot), p) not in _names_cache)
    if need:
        try:
            r = subprocess.run(["go", "list", "-e", "-f", "{{.ImportPath}} {{.Name}}", *need], cwd=modroot,
                               capture_output=True, text=True, timeout=60)
            for ln in (r.stdout or "").splitlines():
                parts = ln.split()
                if len(parts) == 2 and parts[1] not in ("", "<nil>"):
                    _names_cache[(str(modroot), parts[0])] = parts[1]
        except Exception:
            pass
    out = {}
    for p in paths:
        got = _names_cache.get((str(modroot), p))
        if got is None:
            segs = p.split("/")
            last = segs[-1]
            if re.fullmatch(r"v\d+", last) and len(segs) > 1:
                last = segs[-2]
            last = last.split(".")[0] if re.search(r"\.v\d+$", last) else last
            last = re.sub(r"^go-", "", last)
            got = re.sub(r"[^A-Za-z0-9_]", "_", last)
        out[p] = got
    return out


def _mod_of(path: Path):
    for d in [path.parent, *path.parent.parents]:
        gm = d / "go.mod"
        if gm.exists():
            m = re.search(r"(?m)^module\s+(\S+)", gm.read_text(encoding="utf-8", errors="replace"))
            if m:
                return d, m.group(1).strip('"')
            return None
    return None


def _local_decls(src: bytes, node) -> set:
    """Names declared inside a node by parameters, short var declarations, local var/const/type specs, range."""
    out = set()
    for n in _walk(node):
        if n.type in ("parameter_declaration", "variadic_parameter_declaration"):
            out |= {_txt(src, c) for c in n.children if c.type == "identifier"}
        elif n.type in ("short_var_declaration", "range_clause", "receive_statement"):
            left = n.child_by_field_name("left")
            if left is not None:
                out |= {_txt(src, c) for c in _walk(left) if c.type == "identifier"}
        elif n.type in ("var_spec", "const_spec") and n.parent is not None and n.parent.parent is not None \
                and n.parent.parent.type not in ("source_file",) and n.parent.type not in ("source_file",):
            anc = n.parent
            top = anc.type in ("var_declaration", "const_declaration") and anc.parent is not None \
                and anc.parent.type == "source_file"
            top = top or (anc.type in ("var_spec_list", "const_spec_list") and anc.parent.parent is not None
                          and anc.parent.parent.type == "source_file")
            if not top:
                out |= {_txt(src, c) for c in n.children if c.type == "identifier"}
        elif n.type == "type_parameter_declaration":
            out |= {_txt(src, c) for c in n.children if c.type == "identifier"}
    return out


def _members(src: bytes, root, skip: list | None = None) -> tuple:
    """(unexported field / method names declared, field-like names used) outside the skipped byte ranges."""
    declared, used = set(), set()
    for n in _walk(root):
        if skip and any(a <= n.start_byte and n.end_byte <= b for a, b in skip):
            continue
        t, p = n.type, n.parent
        if t == "field_identifier":
            nm = _txt(src, n)
            if p is not None and p.type in ("field_declaration", "method_declaration", "method_elem"):
                if not _is_exported(nm):
                    declared.add(nm)
            else:
                used.add(nm)
        elif t == "identifier" and p is not None and p.type == "literal_element" and p.parent is not None                 and p.parent.type == "keyed_element" and p.parent.children[0].id == p.id:
            used.add(_txt(src, n))
    return declared, used


def _bare_refs(src: bytes, root, names: set, skip: list) -> tuple:
    """Bare identifier uses of any of `names` in a tree (outside the skipped byte ranges): (nodes, ambiguous)."""
    hits, amb = [], False
    for n in _walk(root):
        if n.type not in ("identifier", "type_identifier") or _txt(src, n) not in names:
            continue
        if any(a <= n.start_byte and n.end_byte <= b for a, b in skip):
            continue
        p = n.parent
        if p is not None and p.type == "qualified_type" and p.children[-1].id == n.id:
            continue                                            # pkg.Name: the Name of another package
        if p is not None and p.type == "literal_element" and p.parent is not None and p.parent.type == "keyed_element" \
                and p.parent.children[0].id == p.id:
            amb = True                                          # struct field or map key: can't tell
        if p is not None and p.type in ("const_spec", "var_spec") and p.children[0].type == "identifier" \
                and any(c.id == n.id for c in p.children if c.type == "identifier"):
            continue                                            # the declared name itself
        if p is not None and p.type in ("function_declaration", "type_spec", "type_alias") \
                and p.child_by_field_name("name") is not None and p.child_by_field_name("name").id == n.id:
            continue
        hits.append(n)
    return hits, amb


def _remove_lines(src: bytes, a: int, b: int) -> tuple:
    """Widen [a, b) to whole lines (the line break after it included)."""
    while a > 0 and src[a - 1] in b" \t":
        a -= 1
    if b < len(src) and src[b:b + 2] == b"\r\n":
        b += 2
    elif b < len(src) and src[b:b + 1] == b"\n":
        b += 1
    return a, b


def _drop_imports(src: bytes, paths: set, names: dict) -> bytes:
    """Remove import specs of these paths whose package is no longer referenced."""
    root = _parse_bytes(src)
    used = _qualifiers(src, root)
    imps = _imports(src, root)
    edits, by_decl = [], {}
    for spec, decl, alias, path in imps:
        by_decl.setdefault(decl.id, [decl, 0, 0])[1] += 1
        local = alias or names.get(path, path.split("/")[-1])
        if path in paths and alias not in ("_", ".") and local not in used:
            by_decl[decl.id][2] += 1
            edits.append((spec, decl))
    out = []
    for spec, decl in edits:
        _d, total, gone = by_decl[decl.id]
        if gone == total:
            if all(e[0] != decl.id for e in out):
                out.append((decl.id, *_remove_lines(src, decl.start_byte, decl.end_byte), b""))
        else:
            out.append((spec.id, *_remove_lines(src, spec.start_byte, spec.end_byte), b""))
    return _apply(src, [(a, b, r) for _i, a, b, r in out])


def _add_imports(src: bytes, wanted: list, eol: bytes) -> bytes:
    """wanted: [(alias or None, path)]; adds those not imported yet."""
    root = _parse_bytes(src)
    have = {p for _s, _d, _a, p in _imports(src, root)}
    wanted = [(a, p) for a, p in wanted if p not in have]
    if not wanted:
        return src
    line = lambda a, p: (f"{a} " if a else "").encode() + b'"' + p.encode() + b'"'
    decls = [c for c in root.children if c.type == "import_declaration"]
    block = next((d for d in reversed(decls) if any(x.type == "import_spec_list" for x in d.children)), None)
    if block is not None:
        close = next(x for x in _walk(block) if x.type == ")" and x.parent.type == "import_spec_list")
        pos = close.start_byte
        while pos > 0 and src[pos - 1] in b" \t":
            pos -= 1
        return src[:pos] + b"".join(b"\t" + line(a, p) + eol for a, p in wanted) + src[pos:]
    if decls:
        pos = decls[-1].end_byte
        return src[:pos] + b"".join(eol + b"import " + line(a, p) for a, p in wanted) + src[pos:]
    pc = next(c for c in root.children if c.type == "package_clause")
    body = eol + eol + b"import (" + eol + b"".join(b"\t" + line(a, p) + eol for a, p in wanted) + b")" \
        if len(wanted) > 1 else eol + eol + b"import " + line(*wanted[0])
    return src[:pc.end_byte] + body + src[pc.end_byte:]


def _fresh_alias(base: str, taken: set) -> str:
    n = base
    while n in taken:
        n += "pkg"
    return n


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of, with_dependencies: bool = False) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: Go symbols move one at a time (a type takes its methods along); pass one symbol."
    sym = symbol.strip()
    node, _o = resolve(cx, sym)
    if node is None:                       # consts and vars are 'variable' nodes, which resolve() skips
        where, _s, nm0 = sym.rpartition("::")
        rows = [r for r in cx.execute("SELECT * FROM nodes WHERE type='variable' AND label=?", (nm0.split(".")[-1],)).fetchall()
                if (r["file_path"] or "").lower().endswith(".go")
                and (not where or (r["file_path"] or "").replace("\\", "/").endswith(where.replace("\\", "/")))]
        files = sorted({r["file_path"] for r in rows})
        if len(files) > 1:
            return f"move_symbol: {sym!r} is a variable or constant in several files ({', '.join(files[:4])}); name it as path::name."
        node = rows[0] if rows else None
    if node is None and sym.rpartition("::")[0].endswith(".go"):
        node = {"file_path": sym.rpartition("::")[0], "label": sym.rpartition("::")[2]}
    if node is None:
        return f"move_symbol: no code symbol named {sym!r} in the index. Use search_symbols to find the exact name."
    rel_a = (node["file_path"] or "").replace("\\", "/")
    path_a = file_of([str(r) for r in roots], rel_a)
    if path_a is None:
        return f"move_symbol: {rel_a} is not on disk (index out of date?)."
    path_a = path_a.resolve()
    root = next((r.resolve() for r in roots if str(path_a).startswith(str(r.resolve()))), roots[0].resolve())
    path_b = Path(to_file)
    if not path_b.is_absolute():
        path_b = root / to_file
    path_b = path_b.resolve()
    if path_b.suffix != ".go":
        return "move_symbol: to_file must be a .go file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."
    if path_a.name.endswith("_test.go") or path_b.name.endswith("_test.go"):
        return "move_symbol: Go test files are not supported (as source or target); nothing was written."

    src_a = path_a.read_bytes()
    tree_a = _parse_bytes(src_a)
    label = node["label"]
    name = label.split(".")[-1]
    tops = _tops(src_a, tree_a)
    meths = _methods(src_a, tree_a)
    if "." in label:
        owner = label.rsplit(".", 1)[0].split(".")[-1]
        if any(r == owner and m == name for _c, r, m in meths):
            return (f"move_symbol: {label} is a method; a method stays with its type (move {owner} and its methods go "
                    f"along). Nothing was written.")
    ent = tops.get(name)
    if not ent and any(m == name for _c, _r, m in meths):
        return (f"move_symbol: {name} is a method; a method stays with its type (move the type and its methods go "
                f"along). Nothing was written.")
    if not ent:
        return (f"move_symbol: {name} is not a top-level func, type, const or var of {rel_a}; nothing was written.")
    if len(ent) > 1:
        return f"move_symbol: {name} is declared more than once in {rel_a}; nothing was written."
    kind, decl, n_specs, n_names = ent[0]
    if name in ("init", "main", "_"):
        return f"move_symbol: {name} can't be moved; nothing was written."
    if n_specs > 1 or n_names > 1:
        return (f"move_symbol: {name} is declared together with other names in one const/var/type group; moving one "
                f"of them out is not supported. Nothing was written.")
    if kind == "func" and decl.child_by_field_name("receiver") is not None:
        return "move_symbol: methods can't move alone; nothing was written."

    pkg_a = _pkg_clause(src_a, tree_a)
    dir_a, dir_b = path_a.parent, path_b.parent
    cross = dir_a != dir_b
    eol = b"\r\n" if b"\r\n" in src_a else b"\n"
    imps_a = _imports(src_a, tree_a)
    if any(p == "C" for _s, _d, _a, p in imps_a):
        return f"move_symbol: {rel_a} uses cgo (import \"C\"); nothing was written."

    # the moved pieces: the declaration, plus (for a type) its methods in this file
    pieces = [(_doc_start(src_a, decl), decl.end_byte)]
    moved_names = {name}
    if kind == "type":
        for m, recv, _mn in meths:
            if recv == name:
                pieces.append((_doc_start(src_a, m), m.end_byte))
    if cross and kind == "type":
        for p in _go_files(dir_a):
            if p == path_a or p.parent != dir_a or p.name.endswith("_test.go"):
                continue
            s2 = p.read_bytes()
            if re.search(rb"func\s*\([^)]*\b" + re.escape(name.encode()) + rb"\b[^)]*\)", s2):
                if any(r == name for _c, r, _m in _methods(s2, _parse_bytes(s2))):
                    return (f"move_symbol: {name} has methods in {p.name}; they must move with the type and are not in "
                            f"the same file. Move them next to the type first. Nothing was written.")
    pieces.sort()

    # package of the target
    mod = _mod_of(path_a)
    if cross and mod is None:
        return "move_symbol: no go.mod found above the file; moving across packages needs a module. Nothing was written."
    if path_b.exists():
        src_b0 = path_b.read_bytes()
        pkg_b = _pkg_clause(src_b0, _parse_bytes(src_b0))
    else:
        src_b0, pkg_b = None, ""
    if not cross:
        if src_b0 is not None and pkg_b != pkg_a:
            return f"move_symbol: {path_b.name} is package {pkg_b}, not {pkg_a}; nothing was written."
        pkg_b = pkg_a
    else:
        if mod is None:
            return "move_symbol: no go.mod."
        modroot, modpath = mod
        try:
            rel_dir_b = dir_b.relative_to(modroot).as_posix()
            rel_dir_a = dir_a.relative_to(modroot).as_posix()
        except ValueError:
            return "move_symbol: the target must be inside the same Go module; nothing was written."
        if not pkg_b:
            for p in _go_files(dir_b):
                if p.parent == dir_b and not p.name.endswith("_test.go"):
                    s2 = p.read_bytes()
                    pkg_b = _pkg_clause(s2, _parse_bytes(s2))
                    break
        if not pkg_b:
            pkg_b = re.sub(r"[^A-Za-z0-9_]", "_", dir_b.name)
        if pkg_a == "main" or pkg_b == "main":
            return "move_symbol: a main package can't be imported; nothing was written."
        old_path = modpath + ("" if rel_dir_a == "." else "/" + rel_dir_a)
        new_path = modpath + ("" if rel_dir_b == "." else "/" + rel_dir_b)

    old_tops: dict = {}
    pkg_files: list = []
    for p in _go_files(dir_a):
        if p.parent != dir_a:
            continue
        s2 = src_a if p == path_a else p.read_bytes()
        t2 = tree_a if p == path_a else _parse_bytes(s2)
        pkg_files.append((p, s2, t2, _pkg_clause(s2, t2)))
    for p, s2, t2, pk in pkg_files:
        if pk == pkg_a and not p.name.endswith("_test.go"):
            for nm in _tops(s2, t2):
                if nm != "_":
                    old_tops.setdefault(nm, []).append(p)

    deps_moved: list = []
    if cross and with_dependencies:
        # unexported helpers of this file that only the moved code uses leave with it
        hd = b"package x\n"
        grew = True
        while grew:
            grew = False
            used = set()
            for a, b in pieces:
                sub = hd + src_a[a:b]
                used |= {_txt(sub, n) for n in _walk(_parse_bytes(sub)) if n.type in ("identifier", "type_identifier")}
            for d in sorted((used & set(tops)) - moved_names):
                ent_d = tops[d]
                if len(ent_d) != 1 or _is_exported(d) or d in ("init", "main", "_"):
                    continue
                k_d, node_d, nsp_d, nn_d = ent_d[0]
                if nsp_d > 1 or nn_d > 1 or (k_d == "func" and node_d.child_by_field_name("receiver") is not None):
                    continue
                if any(f != path_a for f in old_tops.get(d, [])):
                    continue
                extra = [(_doc_start(src_a, node_d), node_d.end_byte)]
                if k_d == "type":
                    if any(r == d for p2, s3, t3, pk3 in pkg_files if p2 != path_a for _c, r, _m in _methods(s3, t3)):
                        continue
                    extra += [(_doc_start(src_a, m), m.end_byte) for m, r, _mn in meths if r == d]
                new_pieces = sorted(pieces + extra)
                if any(_bare_refs(s3, t3, {d}, new_pieces if p3 == path_a else [])[0] for p3, s3, t3, pk3 in pkg_files
                       if pk3 == pkg_a):
                    continue                            # something else in the package still needs it
                pieces, grew = new_pieces, True
                moved_names.add(d)
                deps_moved.append(d)

    if cross:
        # unexported fields / methods are reachable only inside the old package
        moved_decl, moved_used = set(), set()
        for a, b in pieces:
            seg_src = b"package x\n" + src_a[a:b]
            d1, u1 = _members(seg_src, _parse_bytes(seg_src))
            moved_decl |= d1
            moved_used |= u1
        other_decl, other_used = set(), set()
        for p, s2, t2, pk in pkg_files:
            if pk != pkg_a:
                continue
            d2, u2 = _members(s2, t2, pieces if p == path_a else None)
            other_decl |= d2
            other_used |= u2
        hit = sorted((moved_used & other_decl) - moved_decl)
        if hit:
            return (f"move_symbol: {name} may read unexported fields or methods of {pkg_a} types ({', '.join(hit[:5])}), "
                    f"which {pkg_b} can't reach. Nothing was written.")
        hit = sorted(moved_decl & other_used)
        if hit:
            return (f"move_symbol: {pkg_a} code reads the unexported fields or methods {', '.join(hit[:5])} of {name}, "
                    f"which would no longer be reachable after the move. Nothing was written.")

    # --- the moved text; across packages the old package's identifiers it uses get a qualifier
    head = b"package x\n"
    segs, need_old, old_alias = [], False, None
    if cross:
        taken = set()
        for a, b in pieces:
            seg_src = head + src_a[a:b]
            taken |= {_txt(seg_src, n) for n in _walk(_parse_bytes(seg_src))
                      if n.type in ("identifier", "type_identifier", "package_identifier")}
        old_alias = None if pkg_a not in taken else _fresh_alias(pkg_a, taken)
    for a, b in pieces:
        seg = src_a[a:b]
        if cross:
            seg_src = head + seg
            seg_root = _parse_bytes(seg_src)
            refs, amb = _bare_refs(seg_src, seg_root, set(old_tops) - moved_names, [])
            if amb:
                return (f"move_symbol: {name} uses an old-package identifier as a composite literal key (struct field or "
                        f"map key, which can't be told apart); nothing was written.")
            locals_ = _local_decls(seg_src, seg_root)
            bad = next((_txt(seg_src, r) for r in refs if _txt(seg_src, r) in locals_), None)
            if bad:
                return f"move_symbol: {name} declares a local named like the package-level {bad}; nothing was written."
            unexp = sorted({_txt(seg_src, r) for r in refs if not _is_exported(_txt(seg_src, r))})
            if unexp:
                return (f"move_symbol: {name} uses unexported identifiers of {pkg_a} ({', '.join(unexp[:5])}), which "
                        f"{pkg_b} can't reach. Nothing was written.")
            if refs:
                need_old = True
                q = ((old_alias or pkg_a) + ".").encode()
                seg = _apply(seg, [(r.start_byte - len(head), r.start_byte - len(head), q) for r in refs])
        segs.append(seg)
    chunk = (eol + eol).join(sg.rstrip(b"\r\n") for sg in segs) + eol

    # the target must not already have the name
    if cross:
        for p in _go_files(dir_b):
            if p.parent != dir_b or p == path_b:
                continue
            s2 = p.read_bytes()
            if _pkg_clause(s2, _parse_bytes(s2)) == pkg_b and name in _tops(s2, _parse_bytes(s2)):
                return f"move_symbol: package {pkg_b} already declares {name} (in {p.name}); nothing was written."
    if src_b0 is not None and name in _tops(src_b0, _parse_bytes(src_b0)):
        return f"move_symbol: {path_b.name} already declares {name}; nothing was written."
    if any(f != path_a for f in old_tops.get(name, [])):
        return (f"move_symbol: package {pkg_a} declares {name} in more than one file (build tags?); "
                f"nothing was written.")
    for dn in deps_moved:
        if src_b0 is not None and dn in _tops(src_b0, _parse_bytes(src_b0)):
            return f"move_symbol: {path_b.name} already declares {dn}, which {name} needs; nothing was written."
        for p in _go_files(dir_b):
            if p.parent == dir_b and p != path_b:
                s2 = p.read_bytes()
                if _pkg_clause(s2, _parse_bytes(s2)) == pkg_b and dn in _tops(s2, _parse_bytes(s2)):
                    return f"move_symbol: package {pkg_b} already declares {dn}, which {name} needs; nothing was written."

    # imports the moved code needs from A's imports
    chunk_src = head + chunk
    chunk_used = _qualifiers(chunk_src, _parse_bytes(chunk_src))
    names_all = _pkg_names(mod[0] if mod else path_a.parent, {p for _s, _d, _a, p in imps_a}) if imps_a else {}
    carry = []
    for _s, _d, alias, path in imps_a:
        if alias == ".":
            return f"move_symbol: {rel_a} has a dot import ({path}); nothing was written."
        if alias == "_":
            continue
        if (alias or names_all.get(path, path.split("/")[-1])) in chunk_used:
            carry.append((alias, path))
    if cross and need_old:
        carry.append((old_alias, old_path))
    carry_paths = {p for _a, p in carry}

    # --- build B
    new_files: dict = {}
    if src_b0 is None:
        text_b = _add_imports(b"package " + pkg_b.encode() + eol + eol + chunk, carry, eol)
    else:
        rb = _parse_bytes(src_b0)
        for al, pa in carry:
            for _s, _d, a2, p2 in _imports(src_b0, rb):
                nm_b = names_all.get(pa) or pa.split("/")[-1]
                if p2 == pa and (a2 or nm_b) != (al or nm_b):
                    return (f"move_symbol: {path_b.name} imports {pa} as {a2 or nm_b!r}, the moved code uses "
                            f"{al or nm_b!r}; nothing was written.")
        text_b = src_b0 if src_b0.endswith(b"\n") else src_b0 + eol
        text_b = _add_imports(text_b + eol + chunk, carry, eol)

    # --- A without the moved pieces
    out_a = _apply(src_a, [(a, b, b"") for a, b in pieces])
    out_a = re.sub(rb"(\r?\n){3,}", eol + eol, out_a).rstrip(b"\r\n") + eol

    # --- rewrite the uses
    rewritten: list = []
    old_users: list = []

    def alias_for(src: bytes, root) -> tuple:
        """(local name of the new package in this file, whether it still needs an import)"""
        for _s, _d, al, pa in _imports(src, root):
            if pa == new_path:
                return (al or pkg_b), False
        taken = {_txt(src, n) for n in _walk(root) if n.type in ("identifier", "type_identifier", "package_identifier")}
        taken |= {(al or "") for _s, _d, al, _p in _imports(src, root)}
        return (_fresh_alias(pkg_b, taken) if pkg_b in taken else pkg_b), True

    if not cross:
        new_files[path_b] = text_b
        new_files[path_a] = _drop_imports(out_a, carry_paths, names_all)
    else:
        new_files[path_b], new_files[path_a] = text_b, out_a
        for p in _go_files(root):
            if p in (path_a, path_b):
                s2 = new_files[p]
            else:
                s2 = p.read_bytes()
                if name.encode() not in s2:
                    continue
            r2 = _parse_bytes(s2)
            pk = _pkg_clause(s2, r2)
            imps = _imports(s2, r2)
            if any(pa == "C" for _s, _d, _a, pa in imps) and p not in (path_a, path_b):
                return f"move_symbol: {p.name} uses cgo and mentions {name}; nothing was written."
            in_new_pkg = p.parent == dir_b and pk == pkg_b
            edits2, add_new, local_new = [], False, None
            if p.parent == dir_a and pk == pkg_a:                     # bare uses inside the old package
                refs, amb = _bare_refs(s2, r2, {name}, [])
                if refs:
                    if name in _local_decls(s2, r2) or amb:
                        return (f"move_symbol: {p.name} declares a local named {name} or uses it as a composite literal "
                                f"key; nothing was written.")
                    if not _is_exported(name):
                        return (f"move_symbol: {name} is unexported and still used in {p.name} ({pkg_a}); it would no "
                                f"longer be reachable. Nothing was written.")
                    old_users.append(p.name)
                    local_new, add_new = alias_for(s2, r2)
                    edits2 += [(r.start_byte, r.start_byte, (local_new + ".").encode()) for r in refs]
            for _s, _d, al, pa in imps:
                if pa != old_path:
                    continue
                if al == ".":
                    return f"move_symbol: {p.name} dot-imports {pkg_a}; nothing was written."
                loc = al or names_all.get(pa) or _pkg_names(mod[0], {pa})[pa]
                for n in _walk(r2):
                    if n.type == "selector_expression":
                        q, fld = n.child_by_field_name("operand"), n.child_by_field_name("field")
                        if q is None or q.type != "identifier" or fld is None:
                            continue
                    elif n.type == "qualified_type":
                        q, fld = n.children[0], n.children[-1]
                    else:
                        continue
                    if _txt(s2, q) != loc or _txt(s2, fld) != name:
                        continue
                    if in_new_pkg:
                        edits2.append((n.start_byte, n.end_byte, name.encode()))
                    else:
                        if local_new is None:
                            local_new, add_new = alias_for(s2, r2)
                        edits2.append((q.start_byte, q.end_byte, local_new.encode()))
            if not edits2 and p not in (path_a, path_b):
                continue
            body = _apply(s2, edits2)
            if add_new and local_new is not None:
                body = _add_imports(body, [(None if local_new == pkg_b else local_new, new_path)],
                                    b"\r\n" if b"\r\n" in body else b"\n")
            check = {old_path} | (carry_paths if p == path_a else set())
            body = _drop_imports(body, check, {**names_all, old_path: pkg_a})
            new_files[p] = body
            if p not in (path_a, path_b):
                rewritten.append(p)

    if need_old and old_users:
        return (f"move_symbol: {name} uses {pkg_a} and {pkg_a} ({old_users[0]}) uses {name}: after the move {pkg_b} and "
                f"{pkg_a} would import each other (import cycle). Move what {name} depends on first, or move it "
                f"together with its users. Nothing was written.")

    # A left with no declarations: delete it
    ra = _parse_bytes(new_files[path_a])
    delete_a = not any(c.type in ("function_declaration", "method_declaration", "type_declaration", "const_declaration",
                                  "var_declaration") for c in ra.children)

    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")
    if apply:
        for p, data in new_files.items():
            if p == path_a and delete_a:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        if delete_a:
            path_a.unlink()
    verb = "moved" if apply else "would move"
    lines = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)")]
    extra = f" + {len(pieces) - 1} method(s) / helper(s)" if len(pieces) > 1 else ""
    if deps_moved:
        extra += f"; helpers moved along: {', '.join(deps_moved)}"
    if cross:
        lines.append(f"{verb} {name} ({kind}{extra}) from package {pkg_a} to {pkg_b}: {rel_a} -> {rel(path_b)}")
        lines.append(f"import path {old_path} -> {new_path}")
        if rewritten:
            lines.append(f"files updated: {len(rewritten)}")
            lines += [f"  {rel(p)}" for p in rewritten[:12]]
            if len(rewritten) > 12:
                lines.append(f"  ... {len(rewritten) - 12} more")
        lines.append("Comments and strings that mention the old package were not touched. Compile to confirm "
                     "(go build ./... && go vet ./...).")
    else:
        lines.append(f"{verb} {name} ({kind}{extra}) within package {pkg_a}: {rel_a} -> {rel(path_b)}")
        if carry:
            lines.append("imports carried along: " + ", ".join(p for _a, p in carry))
    if apply:
        lines += ["", "Done: the declaration, its imports and every use are already changed. "
                      "Do NOT Read or Edit these files again to redo it.",
                  "Next: check_change, then go build ./... and the related tests."]
    return "\n".join(lines)
