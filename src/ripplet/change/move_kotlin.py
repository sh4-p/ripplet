"""move_symbol for Kotlin: move a top-level class, object, interface, enum, function, property or typealias to another file.

A Kotlin file may hold many top-level declarations, and the declarations of one package see each other without imports.
So moving one to a file of another package means:

* the declaration (with its KDoc) goes to the target file, whose `package` line is kept (a new file gets the package its
  directory names, counted from the source root of the old file);
* `import old.pkg.Name` elsewhere (also `import old.pkg.Name.Member`, `as` aliases) and fully qualified uses name the new
  package; a file of the old package that used the name without an import, or reached it through `import old.pkg.*`,
  gains `import new.pkg.Name`; a file of the new package drops the now redundant import;
* the moved code gets the imports of the old file it uses (a wildcard import always) and an `import old.pkg.X` for each
  top-level name of the old package it used without an import;
* a `private` (file-private) declaration that the old file still uses becomes `internal`.
It refuses (writing nothing) when the name is declared more than once in the file (overloads), when the moved code uses a
`private` declaration of the old file, when the target package already declares the name, or when the package of a new
file can't be told from its directory. Unused imports left behind are only warnings. A file left with no declarations is
deleted; Java callers of top-level functions (`OldFileKt.f()`) are not rewritten.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP = {"node_modules", ".git", "build", "target", "out", ".gradle", ".idea"}
_DECLS = ("class_declaration", "object_declaration", "function_declaration", "property_declaration", "type_alias")
_parser = None


def _parse(src: bytes):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_kotlin as tk
        _parser = ts.Parser(ts.Language(tk.language()))
    return _parser.parse(src).root_node


def _t(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="surrogateescape")


def _walk(n, skip=()):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(c for c in reversed(x.children) if c.type not in skip)


def _kt_files(root: Path) -> list:
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in _SKIP and not d.startswith("."))
        out += [Path(dp) / f for f in sorted(fn) if f.endswith(".kt")]
    return out


def _package(src: bytes, root) -> str:
    for c in root.children:
        if c.type == "package_header":
            q = next((x for x in c.children if x.type == "qualified_identifier"), None)
            return _t(src, q) if q is not None else ""
    return ""


def _decl_name(src: bytes, n):
    if n.type == "property_declaration":
        vds = [c for c in n.children if c.type == "variable_declaration"]
        if len(vds) != 1:
            return None
        ids = [c for c in vds[0].children if c.type == "identifier"]
        return _t(src, ids[0]) if ids else None
    ids = [c for c in n.children if c.type == "identifier"]
    return _t(src, ids[-1] if n.type == "function_declaration" else ids[0]) if ids else None


def _vis(src: bytes, n) -> str:
    for c in n.children:
        if c.type == "modifiers":
            for m in c.children:
                if m.type == "visibility_modifier":
                    return _t(src, m).strip()
    return ""


def _tops(src: bytes, root) -> list:
    return [(_decl_name(src, c), c, _vis(src, c)) for c in root.children if c.type in _DECLS]


def _idents(src: bytes, root) -> set:
    """Identifiers used in the code, not counting the package and import lines."""
    return {_t(src, x) for x in _walk(root, skip=("import", "package_header")) if x.type == "identifier"}


def _doc_start(src: bytes, n) -> int:
    start, prev = n.start_byte, n.prev_sibling
    while prev is not None and prev.type in ("block_comment", "line_comment") and src[prev.end_byte:start].count(b"\n") <= 1:
        start, prev = prev.start_byte, prev.prev_sibling
    return start


_IMP = re.compile(r"(?m)^import\s+([\w.`]+(?:\.\*)?)(?:\s+as\s+(\w+))?[^\S\r\n]*$")


def _imports(text: str) -> list:
    """(line match, dotted path, bound simple name or None for a wildcard)"""
    out = []
    for m in _IMP.finditer(text):
        path = m.group(1)
        out.append((m, path, m.group(2) or (None if path.endswith(".*") else path.split(".")[-1])))
    return out


def _add_imports(text: str, names: list, eol: str) -> str:
    have = {p + (f" as {m.group(2)}" if m.group(2) else "") for m, p, _b in _imports(text)}
    names = [n for n in names if n not in have]
    if not names:
        return text
    block = eol.join(f"import {n}" for n in names)
    ms = list(_IMP.finditer(text))
    if ms:
        e = ms[-1].end()
        return text[:e] + eol + block + text[e:]
    m = re.search(r"(?m)^package\s[^\r\n]*", text)
    if m:
        return text[:m.end()] + eol + eol + block + text[m.end():]
    return block + eol + eol + text


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: Kotlin declarations move one at a time; pass one symbol."
    node, _o = resolve(cx, symbol.strip())
    if node is None:
        where, _s, nm0 = symbol.strip().rpartition("::")
        if where.endswith(".kt"):
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
    if path_b.suffix != ".kt":
        return "move_symbol: to_file must be a .kt file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."

    src_a = path_a.read_bytes()
    root_a = _parse(src_a)
    name = node["label"].split(".")[-1]
    tops = _tops(src_a, root_a)
    hits = [t for t in tops if t[0] == name]
    if not hits:
        return (f"move_symbol: {name} is not a top-level class, object, interface, function, property or typealias of "
                f"{rel_a} (members can't be moved on their own); nothing was written.")
    if len(hits) > 1:
        return f"move_symbol: {name} is declared {len(hits)} times in {rel_a} (overloads?); nothing was written."
    _n, decl, vis = hits[0]
    pkg_a = _package(src_a, root_a)
    text_a = src_a.decode("utf-8", errors="surrogateescape")
    eol = "\r\n" if "\r\n" in text_a else "\n"

    # ---- the package of the target
    src_b0 = path_b.read_bytes() if path_b.exists() else None
    if src_b0 is not None:
        pkg_b = _package(src_b0, _parse(src_b0))
    else:
        dirs = pkg_a.split(".") if pkg_a else []
        parts_a = list(path_a.parent.parts)
        if path_b.parent == path_a.parent:
            pkg_b = pkg_a
        elif dirs and parts_a[len(parts_a) - len(dirs):] == dirs:
            src_root = Path(*parts_a[:len(parts_a) - len(dirs)])
            try:
                rel_dirs = path_b.parent.relative_to(src_root).parts
            except ValueError:
                return f"move_symbol: a new file must be under the source root {src_root.name}; nothing was written."
            pkg_b = ".".join(rel_dirs)
        else:
            return (f"move_symbol: the package of a new file can't be told ({rel_a} does not sit in the directory its package "
                    f"names); move into an existing file of the package. Nothing was written.")
    cross = pkg_b != pkg_a
    old_fq, new_fq = (f"{pkg_a}.{name}" if pkg_a else name), (f"{pkg_b}.{name}" if pkg_b else name)

    # ---- the project: top-level names per package
    files = {}
    by_pkg: dict = {}
    for p in _kt_files(root):
        s = src_a if p == path_a else p.read_bytes()
        try:
            r = _parse(s)
        except Exception:
            continue
        pk = _package(s, r)
        files[p] = (s, r, pk)
        for nm, n, v in _tops(s, r):
            if nm:
                by_pkg.setdefault(pk, {}).setdefault(nm, []).append((p, v))
    if cross and any(p != path_a for p, _v in by_pkg.get(pkg_b, {}).get(name, [])):
        return f"move_symbol: package {pkg_b} already declares {name}; nothing was written."

    # ---- what the moved code needs
    used = _idents(src_a, decl)
    imps_a = _imports(text_a)
    bound_a = {b: p for _m, p, b in imps_a if b}
    remaining_names = {nm: v for nm, n, v in tops if nm and nm != name}
    carry, need = [], []
    for _m, path, b in imps_a:
        if path == old_fq:
            continue
        if b is None or b in used:
            carry.append(path if not _m.group(2) else f"{path} as {_m.group(2)}")
    for nm in sorted(used - {name}):
        if nm in bound_a:
            continue
        if remaining_names.get(nm) == "private":
            return (f"move_symbol: {name} uses {nm}, a private declaration of {rel_a}; make it internal or move it too. "
                    f"Nothing was written.")
        if cross and nm in by_pkg.get(pkg_a, {}):
            decls = by_pkg[pkg_a][nm]
            if all(v == "private" and p != path_a for p, v in decls):
                continue
            if nm in by_pkg.get(pkg_b, {}):
                return f"move_symbol: {name} uses {nm} of {pkg_a}, but {pkg_b} declares a {nm} too; nothing was written."
            need.append(f"{pkg_a}.{nm}" if pkg_a else nm)

    # ---- the moved text
    start, end = _doc_start(src_a, decl), decl.end_byte
    chunk = src_a[start:end].decode("utf-8", errors="surrogateescape")
    if cross:
        chunk = re.sub(r"(?<![\w.])" + re.escape(old_fq) + r"(?!\w)", new_fq, chunk)
    cut_end = end
    while src_a[cut_end:cut_end + 1] in (b" ", bytes([9]), bytes([13]), bytes([10])):
        cut_end += 1
    rest_a = src_a[:start] + src_a[cut_end:]
    rest_root = _parse(rest_a)
    uses_in_a = name in _idents(rest_a, rest_root)
    widened = False
    if vis == "private" and uses_in_a:
        chunk = re.sub(r"\bprivate\b", "internal", chunk, count=1)
        widened = True
    rest_text = rest_a.decode("utf-8", errors="surrogateescape")
    if cross and uses_in_a:
        rest_text = _add_imports(rest_text, [new_fq], eol)
    drop_a = not [t for t in _tops(rest_a, rest_root) if t[0]]

    out_files = {}
    if src_b0 is None:
        head = (f"package {pkg_b}" + eol + eol) if pkg_b else ""
        imps = [x for x in dict.fromkeys(carry + need)]
        body = (eol.join(f"import {i}" for i in imps) + eol + eol) if imps else ""
        out_files[path_b] = (head + body + chunk + eol).encode("utf-8", errors="surrogateescape")
    else:
        text_b = src_b0.decode("utf-8", errors="surrogateescape")
        eol_b = "\r\n" if "\r\n" in text_b else "\n"
        if cross:                             # the target itself may have imported or spelled out the old name
            text_b = re.sub(r"(?m)^(import\s+)" + re.escape(old_fq) + r"(?=[.\s]|$)", lambda m: m.group(1) + new_fq, text_b)
            text_b = re.sub(r"(?<![\w.])" + re.escape(old_fq) + r"(?!\w)", new_fq, text_b)
            text_b = re.sub(r"(?m)^import\s+" + re.escape(new_fq) + r"[^\S\r\n]*\r?\n", "", text_b)
        have = {b: p for _m, p, b in _imports(text_b) if b}
        add = []
        for i in dict.fromkeys(carry + need):
            path, _s, al = i.partition(" as ")
            b = al or path.split(".")[-1]
            if i.endswith(".*"):
                add.append(i)
            elif b in have:
                if have[b] != path:
                    return f"move_symbol: {path_b.name} already imports a different {b}; nothing was written."
            elif not (pkg_b and path == f"{pkg_b}.{b}"):
                add.append(i)
            if al and re.search(r"\b" + re.escape(path.split(".")[-1]) + r"\b", text_b):
                add.append(path)              # an alias hides the original name, which this file also uses
        text_b = _add_imports(text_b, add, eol_b)
        text_b = text_b.rstrip("\r\n") + eol_b + eol_b + chunk.replace(eol, eol_b) + eol_b
        out_files[path_b] = text_b.encode("utf-8", errors="surrogateescape")

    # ---- every other file
    updated, gained = [], []
    fq_rx = re.compile(r"(?<![\w.])" + re.escape(old_fq) + r"(?!\w)")
    imp_rx = re.compile(r"(?m)^(import\s+)" + re.escape(old_fq) + r"(?=[.\s]|$)")
    name_rx = re.compile(r"\b" + re.escape(name) + r"\b")
    for p, (s, r, pk) in files.items():
        if p == path_a or p == path_b:
            continue
        txt = s.decode("utf-8", errors="surrogateescape")
        if not name_rx.search(txt):
            continue
        e = "\r\n" if "\r\n" in txt else "\n"
        out = txt
        if cross:
            out = imp_rx.sub(lambda m: m.group(1) + new_fq, out)
            out = fq_rx.sub(new_fq, out)
            if pk == pkg_b:
                out = re.sub(r"(?m)^import\s+" + re.escape(new_fq) + r"[^\S\r\n]*\r?\n", "", out)
            elif name in _idents(s, r) and not any(b == name for _m, _p, b in _imports(out)):
                star = any(path == f"{pkg_a}.*" for _m, path, _b in imps_of(out)) if pkg_a else False
                if pk == pkg_a or star:
                    out = _add_imports(out, [new_fq], e)
                    gained.append(p)
        if out != txt:
            out_files[p] = out.encode("utf-8", errors="surrogateescape")
            updated.append(p)

    # a Java file naming the Kotlin class by its package must change too
    if apply:
        for p, data in out_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        if drop_a:
            path_a.unlink()
        else:
            path_a.write_bytes(rest_text.encode("utf-8", errors="surrogateescape"))
    rel = lambda q: str(Path(q).resolve().relative_to(root)).replace("\\", "/")
    verb = "moved" if apply else "would move"
    lines = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
             f"{verb} {name} from {pkg_a or '(default)'} to {pkg_b or '(default)'}: {rel_a} -> {rel(path_b)}" +
             ("" if cross else "  (same package: only the file changes)")]
    if drop_a:
        lines.append(f"{rel_a} had nothing else and {'was' if apply else 'would be'} deleted.")
    if widened:
        lines.append(f"{name} was private and {rel_a} still uses it: now internal.")
    if need:
        lines.append("imports added to the moved code (names of the old package): " + ", ".join(n.split(".")[-1] for n in need))
    if updated:
        lines.append(f"files updated: {len(updated)} ({len(gained)} gained an import of {name})")
        lines += [f"  {rel(q)}" for q in updated[:12]]
        if len(updated) > 12:
            lines.append(f"  ... {len(updated) - 12} more")
    lines.append("Java callers of top-level functions (OldFileKt.f()) and non-Kotlin files were not touched; unused imports left behind are warnings.")
    if apply:
        lines += ["", "Done: the declaration, its package and every import are already changed. Do NOT Read or Edit these "
                      "files again to redo it.", "Next: check_change, then compile / run the related tests."]
    return "\n".join(lines)


def imps_of(text: str) -> list:
    return _imports(text)
