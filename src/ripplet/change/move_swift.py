"""move_symbol for Swift: move a top-level class, struct, enum, actor, protocol, function, property or typealias to another file
of the same module.

Every file of a Swift module sees the others' internal declarations, so inside one module a move changes no reference. What
can break is:

* the imports: the moved code gets the old file's `import` lines;
* file-scope access: a `private` / `fileprivate` declaration that the old file still uses loses the modifier (internal); the
  moved code using a `private` / `fileprivate` declaration of the old file is refused, and so is a type with `private`
  members that an `extension` left in the old file reaches;
* `main.swift` top-level code (globals there initialise in order), and a move to a file of another SwiftPM target (that needs
  `public` and a new `import`).
A file left with only imports and comments is deleted.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP = {".git", ".build", "build", "Pods", "Carthage", "DerivedData", "node_modules", ".swiftpm"}
_parser = None


def _parse(src: bytes):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_swift as tw
        _parser = ts.Parser(ts.Language(tw.language()))
    return _parser.parse(src).root_node


def _t(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="surrogateescape")


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _swift_files(root: Path) -> list:
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in _SKIP and not d.startswith("."))
        out += [Path(dp) / f for f in sorted(fn) if f.endswith(".swift")]
    return out


def _mods(src: bytes, n) -> str:
    m = next((c for c in n.children if c.type == "modifiers"), None)
    return _t(src, m) if m is not None else ""


def _decl_name(src: bytes, n):
    if n.type == "class_declaration":
        if any(c.type == "extension" for c in n.children):
            return None
        x = next((c for c in n.children if c.type == "type_identifier"), None)
        return _t(src, x) if x is not None else None
    if n.type in ("protocol_declaration", "typealias_declaration"):
        x = next((c for c in n.children if c.type == "type_identifier"), None)
        return _t(src, x) if x is not None else None
    if n.type == "function_declaration":
        x = next((c for c in n.children if c.type == "simple_identifier"), None)
        return _t(src, x) if x is not None else None
    if n.type == "property_declaration":
        pats = [c for c in n.children if c.type == "pattern"]
        if len(pats) != 1:
            return None
        ids = [x for x in _walk(pats[0]) if x.type == "simple_identifier"]
        return _t(src, ids[0]) if len(ids) == 1 else None
    return None


_DECLS = ("class_declaration", "protocol_declaration", "typealias_declaration", "function_declaration", "property_declaration")


def _tops(src: bytes, root) -> list:
    return [(_decl_name(src, c), c) for c in root.children if c.type in _DECLS]


def _idents(src: bytes, n) -> set:
    return {_t(src, x) for x in _walk(n) if x.type in ("simple_identifier", "type_identifier")}


def _doc_start(src: bytes, n) -> int:
    start, prev = n.start_byte, n.prev_sibling
    while prev is not None and prev.type in ("comment", "multiline_comment") and src[prev.end_byte:start].count(b"\n") <= 1:
        start, prev = prev.start_byte, prev.prev_sibling
    return start


def _is_file_private(mods: str) -> bool:
    return bool(re.search(r"\b(private|fileprivate)\b(?!\()", mods))


def _target(path: Path):
    parts = path.parts
    for key in ("Sources", "Tests"):
        if key in parts:
            i = parts.index(key)
            if i + 1 < len(parts) - 1:
                return (key, parts[i + 1])
    return None


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: Swift declarations move one at a time; pass one symbol."
    node, _o = resolve(cx, symbol.strip())
    if node is None:
        where, _s, nm0 = symbol.strip().rpartition("::")
        if where.endswith(".swift"):
            node = {"file_path": where, "label": nm0}
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
    if path_b.suffix != ".swift":
        return "move_symbol: to_file must be a .swift file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."
    if _target(path_a) != _target(path_b):
        return (f"move_symbol: {rel_a} and the target are in different modules (SwiftPM targets); that needs `public` and a new "
                f"import, which is not supported. Nothing was written.")

    src_a = path_a.read_bytes()
    root_a = _parse(src_a)
    name = re.split(r"[.:]", node["label"])[-1]
    tops = _tops(src_a, root_a)
    hits = [t for t in tops if t[0] == name]
    if not hits:
        return (f"move_symbol: {name} is not a top-level class, struct, enum, actor, protocol, function, property or typealias "
                f"of {rel_a}; nothing was written.")
    if len(hits) > 1:
        return f"move_symbol: {name} is declared {len(hits)} times in {rel_a} (overloads?); nothing was written."
    _n, decl = hits[0]
    kind = decl.type
    if decl.type == "property_declaration" and "main.swift" in (path_a.name, path_b.name):
        return "move_symbol: top-level variables of main.swift initialise in order; moving one is not supported. Nothing was written."
    mods = _mods(src_a, decl)
    src_b0 = path_b.read_bytes() if path_b.exists() else None
    if src_b0 is not None and any(t[0] == name for t in _tops(src_b0, _parse(src_b0))):
        return f"move_symbol: {path_b.name} already declares {name}; nothing was written."
    eol = b"\r\n" if b"\r\n" in src_a else b"\n"
    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")

    # ---- cut
    start = _doc_start(src_a, decl)
    end = decl.end_byte
    chunk = src_a[start:end]
    cut_end = end
    while src_a[cut_end:cut_end + 1] in (b" ", bytes([9]), bytes([13]), bytes([10])):
        cut_end += 1
    rest = src_a[:start] + src_a[cut_end:]
    rest_root = _parse(rest)
    rest_idents = set()
    for c in rest_root.children:
        if c.type != "import_declaration":
            rest_idents |= _idents(rest, c)

    # ---- access
    others = {t[0]: t[1] for t in tops if t[0] and t[0] != name}
    for nm in sorted(_idents(src_a, decl)):
        if nm in others and _is_file_private(_mods(src_a, others[nm])):
            return (f"move_symbol: {name} uses {nm}, a private or fileprivate declaration of {rel_a}; make it internal or move "
                    f"it too. Nothing was written.")
    widened = False
    if _is_file_private(mods) and name in rest_idents:
        chunk = re.sub(rb"\b(?:private|fileprivate)\b(?!\()\s*", b"", chunk, count=1)
        widened = True
    if kind == "class_declaration" and re.search(rb"\bprivate\b|\bfileprivate\b", chunk):
        for c in root_a.children:
            if c.type == "class_declaration" and any(x.type == "extension" for x in c.children):
                ut = next((x for x in c.children if x.type == "user_type"), None)
                if ut is not None and _t(src_a, ut) == name:
                    return (f"move_symbol: {name} has private members and {rel_a} keeps an `extension {name}` that can reach "
                            f"them only while both stay in one file; nothing was written.")

    # ---- imports
    imports = [_t(src_a, c).strip() for c in root_a.children if c.type == "import_declaration"]
    text_a = rest.decode("utf-8", errors="surrogateescape")
    drop_a = not [c for c in rest_root.children if c.type not in ("import_declaration", "comment", "multiline_comment")]
    text_chunk = chunk.decode("utf-8", errors="surrogateescape")
    if src_b0 is None:
        head = (eol.decode().join(imports) + eol.decode() * 2) if imports else ""
        out_b = (head + text_chunk.rstrip("\r\n") + eol.decode()).encode("utf-8", errors="surrogateescape")
    else:
        tb = src_b0.decode("utf-8", errors="surrogateescape")
        eb = "\r\n" if "\r\n" in tb else "\n"
        have = {_t(src_b0, c).strip() for c in _parse(src_b0).children if c.type == "import_declaration"}
        add = [i for i in imports if i not in have]
        if add:
            rb = _parse(src_b0)
            lasts = [c for c in rb.children if c.type == "import_declaration"]
            ins = eb.join(add)
            if lasts:
                pos = lasts[-1].end_byte
                tb = tb[:len(src_b0[:pos].decode("utf-8", errors="surrogateescape"))] + eb + ins + tb[len(src_b0[:pos].decode("utf-8", errors="surrogateescape")):]
            else:
                tb = ins + eb + eb + tb
        out_b = (tb.rstrip("\r\n") + eb + eb + text_chunk.rstrip("\r\n").replace(eol.decode(), eb) + eb).encode("utf-8", errors="surrogateescape")

    out_files = {path_b: out_b}
    if apply:
        for p, data in out_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        if drop_a:
            path_a.unlink()
        else:
            path_a.write_bytes(rest)
    verb = "moved" if apply else "would move"
    lines = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
             f"{verb} {name}: {rel_a} -> {rel(path_b)} (same module: no reference changes)"]
    if drop_a:
        lines.append(f"{rel_a} had nothing else and {'was' if apply else 'would be'} deleted.")
    if widened:
        lines.append(f"{name} was private/fileprivate and {rel_a} still uses it: now internal.")
    lines.append("Imports of the old file were copied; unused imports are not an error in Swift.")
    if apply:
        lines += ["", "Done: the declaration and its imports are already moved. Do NOT Read or Edit these files again to redo it.",
                  "Next: build / run the tests."]
    return "\n".join(lines)
