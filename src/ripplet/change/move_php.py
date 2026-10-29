"""move_symbol for PHP: move a class, interface, trait or enum to another file (and namespace).

PHP ties a class to a namespace and, with PSR-4 autoloading, the namespace to a directory (composer.json `autoload`).
So "move" means: the file goes to the new directory, its `namespace` line follows the PSR-4 mapping of that directory
(an existing target file keeps its own namespace), and every name that pointed at the old fully qualified name changes:

* `use Old\\Name;` / `use Old\\{Name, X};` and fully qualified `\\Old\\Name` (code, docblocks, `'Old\\Name'` and
  `"Old\\\\Name"` strings) become the new name;
* a file of the old namespace that used `Name` bare gets `use New\\Name;`;
* a partially qualified use (`Models\\Name` relative to the namespace, or through a `use Old;` namespace alias)
  becomes the fully qualified new name;
* the moved class gets `use Old\\Sibling;` for each class of the old namespace it named without a `use`, and
  partially qualified names in it are made fully qualified.
It refuses (writing nothing) bracketed namespaces, a file with several class-likes or other top-level code, a global
(namespace-less) class, a new file not named after the class, a name that already exists in the new namespace, and a
grouped `use` of a parent namespace that reaches the class.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_SKIP = {"vendor", "node_modules", "storage", ".git", "cache"}
_CLASSLIKE = ("class_declaration", "interface_declaration", "trait_declaration", "enum_declaration")
_BUILTIN = {"self", "static", "parent", "array", "callable", "iterable", "mixed", "null", "void", "never", "bool",
            "boolean", "int", "integer", "float", "double", "string", "object", "false", "true", "resource"}
_parser = None


def _parse_bytes(src: bytes):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_php as tp
        _parser = ts.Parser(ts.Language(tp.language_php()))
    return _parser.parse(src).root_node


def _txt(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _php_files(root: Path) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("."))
        out += [Path(dirpath) / f for f in sorted(filenames) if f.endswith(".php")]
    return out


def _namespace(src: bytes, root):
    """(name, node, bracketed) of the file's namespace; name is '' when there is none."""
    for c in root.children:
        if c.type == "namespace_definition":
            nm = next((x for x in c.children if x.type == "namespace_name"), None)
            bracketed = any(x.type == "compound_statement" for x in c.children)
            return (_txt(src, nm) if nm is not None else ""), c, bracketed
    return "", None, False


def _classlikes(src: bytes, root) -> list:
    out = []
    for c in root.children:
        if c.type in _CLASSLIKE:
            out.append((_txt(src, c.child_by_field_name("name")), c))
    return out


def _imports(src: bytes, root) -> list:
    """Class `use` clauses: [(alias, fqcn, clause node, decl node, group prefix or None)]."""
    out = []
    for d in root.children:
        if d.type != "namespace_use_declaration":
            continue
        head = _txt(src, d)[:20].lower()
        if re.match(r"use\s+(function|const)\b", head):
            continue
        group = next((x for x in d.children if x.type == "namespace_use_group"), None)
        prefix = None
        if group is not None:
            pn = next((x for x in d.children if x.type in ("namespace_name", "qualified_name", "name")), None)
            prefix = _txt(src, pn).strip("\\") if pn is not None else ""
        clauses = [x for x in (group.children if group is not None else d.children) if x.type == "namespace_use_clause"]
        for cl in clauses:
            nm = next((x for x in cl.children if x.type in ("qualified_name", "name")), None)
            if nm is None:
                continue
            fq = _txt(src, nm).strip("\\")
            if prefix is not None:
                fq = prefix + "\\" + fq
            names = [x for x in cl.children if x.type == "name"]
            alias = _txt(src, names[-1]) if any(x.type == "as" for x in cl.children) and names else fq.split("\\")[-1]
            out.append((alias, fq, cl, d, prefix))
    return out


_REF_PARENTS = ("named_type", "base_clause", "class_interface_clause", "object_creation_expression", "attribute",
                "binary_expression", "class_constant_access_expression", "scoped_call_expression",
                "scoped_property_access_expression", "use_declaration")


def _class_refs(src: bytes, root) -> list:
    """Nodes naming a class: [(node, text)]."""
    out = []
    for n in _walk(root):
        if n.type not in ("name", "qualified_name"):
            continue
        p = n.parent
        if p is None or p.type not in _REF_PARENTS:
            continue
        if p.type in ("class_constant_access_expression", "scoped_call_expression", "scoped_property_access_expression")                 and p.children[0].id != n.id:
            continue
        if p.type == "binary_expression" and not (n.prev_sibling is not None and n.prev_sibling.type == "instanceof"):
            continue
        if p.type == "object_creation_expression" and not (p.children[1].id == n.id):
            continue
        if p.type == "use_declaration" and (p.parent is None or p.parent.type != "declaration_list"):
            continue
        t = _txt(src, n)
        if t.lstrip("\\").lower() in _BUILTIN or "\\" not in t and t.lower() in _BUILTIN:
            continue
        out.append((n, t))
    return out


def _resolve(text: str, ns: str, imps: dict) -> tuple:
    """(fully qualified name, how): how is fq / import / import_prefix / bare / relative."""
    if text.startswith("\\"):
        return text[1:], "fq"
    parts = text.split("\\")
    first = imps.get(parts[0]) or imps.get(parts[0].lower())
    if first is not None:
        return "\\".join([first] + parts[1:]), "import" if len(parts) == 1 else "import_prefix"
    return (ns + "\\" if ns else "") + text, "bare" if len(parts) == 1 else "relative"


def _apply(src: bytes, edits: list) -> bytes:
    for a, b, rep in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        src = src[:a] + rep + src[b:]
    return src


def _insert_uses(src: bytes, fqcns: list, eol: bytes):
    """Add `use X;` lines. Returns (bytes, error or None)."""
    root = _parse_bytes(src)
    ns, nsnode, _br = _namespace(src, root)
    imps = _imports(src, root)
    have = {a: f for a, f, *_ in imps}
    declared = {n for n, _c in _classlikes(src, root)}
    lines = []
    for fq in sorted(set(fqcns)):
        last = fq.split("\\")[-1]
        if "\\".join(fq.split("\\")[:-1]) == ns:
            continue
        if have.get(last) == fq:
            continue
        if last in have or last in declared:
            return src, f"{last} is already taken in the target file (use {have.get(last) or 'a class of the same name'})"
        lines.append(f"use {fq};".encode())
    if not lines:
        return src, None
    block = eol.join(lines)
    uses = [d for d in root.children if d.type == "namespace_use_declaration"]
    if uses:
        pos = uses[-1].end_byte
        return src[:pos] + eol + block + src[pos:], None
    if nsnode is not None:
        pos = nsnode.end_byte
        return src[:pos] + eol + eol + block + src[pos:], None
    return src, "no namespace to hang the use statements on"


def _psr4(start: Path) -> tuple:
    """(composer dir, {prefix: [dirs]}) from the nearest composer.json above start."""
    for d in [start, *start.parents]:
        cj = d / "composer.json"
        if cj.exists():
            try:
                data = json.loads(cj.read_text(encoding="utf-8", errors="replace"))
            except ValueError:
                return d, {}
            maps = {}
            for sect in ("autoload", "autoload-dev"):
                for pre, dirs in ((data.get(sect) or {}).get("psr-4") or {}).items():
                    maps.setdefault(pre, []).extend([dirs] if isinstance(dirs, str) else list(dirs))
            return d, maps
    return start, {}


def _ns_for_dir(comp: Path, maps: dict, directory: Path):
    """PSR-4 namespace of a directory, or None when no mapping covers it."""
    best = None
    for pre, dirs in maps.items():
        for dd in dirs:
            base = (comp / dd).resolve()
            try:
                rel = directory.resolve().relative_to(base)
            except ValueError:
                continue
            ns = (pre.rstrip("\\") + ("\\" + "\\".join(rel.parts) if rel.parts else "")).strip("\\")
            if best is None or len(base.parts) > best[0]:
                best = (len(base.parts), ns)
    return best[1] if best else None


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: PHP classes move one at a time (each is its own file); pass one symbol."
    node, _o = resolve(cx, symbol.strip())
    if node is None:
        return f"move_symbol: no code symbol named {symbol!r} in the index. Use search_symbols to find the exact name."
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
    if path_b.suffix != ".php":
        return "move_symbol: to_file must be a .php file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."

    src_a = path_a.read_bytes()
    tree_a = _parse_bytes(src_a)
    name = node["label"].split(".")[-1]
    ns_a, nsnode_a, bracketed = _namespace(src_a, tree_a)
    if bracketed:
        return f"move_symbol: {rel_a} uses a bracketed namespace block; nothing was written."
    if not ns_a:
        return f"move_symbol: {name} is in the global namespace; nothing was written."
    cls = _classlikes(src_a, tree_a)
    hit = next((c for c in cls if c[0] == name), None)
    if hit is None:
        return f"move_symbol: {name} is not a top-level class, interface, trait or enum of {rel_a}; nothing was written."
    if len(cls) > 1:
        return (f"move_symbol: {rel_a} declares {len(cls)} class-likes ({', '.join(c[0] for c in cls)}); moving one of "
                f"them out is not supported. Nothing was written.")
    other_top = [c.type for c in tree_a.children if c.type not in (
        "php_tag", "namespace_definition", "namespace_use_declaration", "comment", "declare_statement", "text", "text_interpolation",
        *_CLASSLIKE)]
    if other_top:
        return f"move_symbol: {rel_a} has top-level code besides the class ({other_top[0]}); nothing was written."

    comp, maps = _psr4(path_a.parent)
    src_b0 = path_b.read_bytes() if path_b.exists() else None
    if src_b0 is not None:
        tree_b0 = _parse_bytes(src_b0)
        ns_b, _nb, br_b = _namespace(src_b0, tree_b0)
        if br_b or not ns_b:
            return f"move_symbol: {path_b.name} has no plain namespace to move into; nothing was written."
        if any(c[0] == name for c in _classlikes(src_b0, tree_b0)):
            return f"move_symbol: {path_b.name} already declares {name}; nothing was written."
    else:
        if path_b.stem != name:
            return f"move_symbol: the new file must be named {name}.php (PSR-4 ties file and class names); nothing was written."
        mapped = _ns_for_dir(comp, maps, path_b.parent)
        ns_b = mapped if mapped is not None else ns_a
    old_fq, new_fq = f"{ns_a}\\{name}", f"{ns_b}\\{name}"
    changed_ns = ns_b != ns_a
    eol = b"\r\n" if b"\r\n" in src_a else b"\n"

    files = {}
    declared = {}
    for p in _php_files(root):
        s = src_a if p == path_a else p.read_bytes()
        t = tree_a if p == path_a else _parse_bytes(s)
        files[p] = (s, t)
        n2, _nd, _b = _namespace(s, t)
        for cn, _c in _classlikes(s, t):
            declared.setdefault((n2 + "\\" + cn) if n2 else cn, p)
    if new_fq in declared and declared[new_fq] != path_a:
        return f"move_symbol: {new_fq} already exists ({declared[new_fq].name}); nothing was written."

    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")
    old_rx = re.compile(r"(?<![\w\\])(\\?)" + re.escape(old_fq) + r"(?![\w\\])")
    old_rx2 = re.compile(r"(?<![\w\\])(\\\\)?" + re.escape(old_fq.replace("\\", "\\\\")) + r"(?![\w\\])")
    new_single = new_fq
    new_double = new_fq.replace("\\", "\\\\")

    def retarget(text: bytes) -> bytes:
        s = text.decode("utf-8", errors="surrogateescape")
        s = old_rx2.sub(lambda m: (m.group(1) or "") + new_double, s)
        s = old_rx.sub(lambda m: m.group(1) + new_single, s)
        return s.encode("utf-8", errors="surrogateescape")

    # --- other files
    out_files: dict = {}
    updated = []
    for p, (s, t) in files.items():
        if p == path_a:
            continue
        if name.encode() not in s and old_fq.replace("\\", "\\\\").encode() not in s:
            continue
        ns_f, _nd, br = _namespace(s, t)
        imps = _imports(s, t)
        alias_map = {a: f for a, f, *_ in imps}
        edits, need_use = [], False
        for n, tx in _class_refs(s, t):
            fq, how = _resolve(tx, ns_f, alias_map)
            if fq != old_fq:
                continue
            if how in ("import_prefix", "relative"):
                edits.append((n.start_byte, n.end_byte, b"\\" + new_fq.encode()))
            elif how == "bare" and ns_f == ns_a and changed_ns:
                need_use = True
        for a, f, cl, d, prefix in imps:
            if prefix is not None and f == old_fq:
                if prefix != ns_a:
                    return f"move_symbol: {rel(p)} reaches {name} through a grouped use of a parent namespace; nothing was written."
                grp = next(x for x in d.children if x.type == "namespace_use_group")
                keep = [x for x in grp.children if x.type == "namespace_use_clause" and x.id != cl.id]
                txt_cl = _txt(s, cl)
                head = f"use {prefix}\\{{" + ", ".join(_txt(s, k) for k in keep) + "};" if len(keep) > 1 else (
                    f"use {prefix}\\{_txt(s, keep[0])};" if keep else "")
                moved_line = f"use {ns_b}\\{txt_cl};"
                repl = (head.encode() + eol if head else b"") + moved_line.encode()
                edits.append((d.start_byte, d.end_byte, repl))
        body = _apply(s, edits)
        body = retarget(body)
        if need_use:
            body, err = _insert_uses(body, [new_fq], b"\r\n" if b"\r\n" in body else b"\n")
            if err:
                return f"move_symbol: {rel(p)}: {err}; nothing was written."
        if ns_f == ns_b:           # `use New\Name;` inside New itself is redundant
            body = re.sub(rb"(?m)^use " + re.escape(new_fq.encode()) + rb";[^\S\r\n]*\r?\n", b"", body)
        if body != s:
            out_files[p] = body
            updated.append(p)

    # --- the moved class
    imps_a = _imports(src_a, tree_a)
    amap = {a: f for a, f, *_ in imps_a}
    edits_a, need = [], []
    for n, tx in _class_refs(src_a, tree_a):
        fq, how = _resolve(tx, ns_a, amap)
        if how == "relative":
            edits_a.append((n.start_byte, n.end_byte, b"\\" + fq.encode())) if changed_ns else None
        elif how == "bare" and changed_ns and fq != old_fq and fq in declared:
            need.append(fq)
    body_a = _apply(src_a, edits_a)
    body_a = retarget(body_a)
    tree_x = _parse_bytes(body_a)
    if src_b0 is None:
        _ns, nsn, _b = _namespace(body_a, tree_x)
        nm = next(x for x in nsn.children if x.type == "namespace_name")
        text_b = body_a[:nm.start_byte] + ns_b.encode() + body_a[nm.end_byte:]
        # a use of the class's own new namespace is redundant
        text_b = re.sub(rb"(?m)^use " + re.escape(ns_b.encode()) + rb"\\[A-Za-z_]\w*;[^\S\r\n]*\r?\n", b"", text_b)
    else:
        cdecl = next(c for c in tree_x.children if c.type in _CLASSLIKE)
        start, prev = cdecl.start_byte, cdecl.prev_sibling
        while prev is not None and prev.type == "comment" and body_a[prev.end_byte:start].count(b"\n") <= 1:
            start, prev = prev.start_byte, prev.prev_sibling
        cls_text = body_a[start:cdecl.end_byte]
        text_b = src_b0 if src_b0.endswith(b"\n") else src_b0 + eol
        text_b = text_b + eol + cls_text + eol
        need += [f for _a, f, *_ in imps_a if f != old_fq]       # what the class imported goes into the target file
    text_b, err = _insert_uses(text_b, need, eol)
    if err:
        return f"move_symbol: {err}; nothing was written."
    out_files[path_b] = text_b

    if apply:
        for p, data in out_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        path_a.unlink()
    verb = "moved" if apply else "would move"
    lines = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
             f"{verb} {name} from {ns_a} to {ns_b}: {rel_a} -> {rel(path_b)}" +
             ("" if changed_ns else "  (namespace unchanged: only the file moves)")]
    if not changed_ns and src_b0 is None and _ns_for_dir(comp, maps, path_b.parent) is None:
        lines.append("no composer.json psr-4 entry covers the new directory: the namespace was kept.")
    if updated:
        lines.append(f"files updated: {len(updated)}")
        lines += [f"  {rel(p)}" for p in updated[:12]]
        if len(updated) > 12:
            lines.append(f"  ... {len(updated) - 12} more")
    lines.append("Dynamic class names (string concatenation, class_alias) and non-PHP files were not touched.")
    if apply:
        lines += ["", "Done: the file, its namespace line and every reference are already changed. "
                      "Do NOT Read or Edit these files again to redo it.",
                  "Next: composer dump-autoload (PSR-4 finds it by path), check_change, then run the tests."]
    return "\n".join(lines)
