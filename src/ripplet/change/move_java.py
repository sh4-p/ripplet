"""move_symbol for Java: move a top-level class, interface, enum or record to another package.

A Java type lives in a file named after it, in a directory named after its package, so "move" means: the file goes
to the new directory, its `package` line changes, and everything that named the old package changes with it:

* `import old.pkg.Type;` / `import static old.pkg.Type.X;` elsewhere name the new package; fully qualified uses
  (`old.pkg.Type` in code, javadoc or a string such as `Class.forName`) are rewritten the same way;
* a file of the old package that used `Type` without an import (same package) gets `import new.pkg.Type;`, and so
  does a file that reached it through `import old.pkg.*;`;
* the moved type gets an import for each type of the old package it used without one;
* imports of the old file stay (an unused import is legal Java); a file of the new package that imported the type
  drops that now redundant import.
It refuses (writing nothing) when the file declares more than one top-level type, when the target exists, when the
moved type uses a package-private type of its old package, or when the new package already has a type of that name
in a file that would be shadowed.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP = {"node_modules", ".git", "build", "target", "out", ".gradle", ".idea"}
_TYPES = ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
          "annotation_type_declaration")
_parser = None


def _parse(path: Path):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_java as tj
        _parser = ts.Parser(ts.Language(tj.language()))
    src = path.read_bytes()
    return src, _parser.parse(src).root_node


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _txt(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="surrogateescape")


def _package(src: bytes, root) -> str:
    for c in root.children:
        if c.type == "package_declaration":
            ids = [x for x in c.children if x.type in ("scoped_identifier", "identifier")]
            return _txt(src, ids[0]) if ids else ""
    return ""


def _tops(src: bytes, root) -> list:
    out = []
    for c in root.children:
        if c.type in _TYPES:
            nm = c.child_by_field_name("name")
            mods = next((x for x in c.children if x.type == "modifiers"), None)
            out.append((_txt(src, nm), c, mods is not None and b"public" in src[mods.start_byte:mods.end_byte].split()))
    return out


def _idents(src: bytes, node) -> set:
    return {_txt(src, x) for x in _walk(node) if x.type in ("identifier", "type_identifier")}


def _hidden_members(src: bytes, tnode) -> set:
    """Names of fields, methods and nested types a class declares without `public` or `private`
    (package-private or protected): reachable only from its own package (or subclasses)."""
    kind = tnode.type
    if kind in ("interface_declaration", "annotation_type_declaration"):
        return set()
    body = tnode.child_by_field_name("body")
    out = set()
    for m in (body.children if body is not None else []):
        if m.type not in ("field_declaration", "method_declaration", "constructor_declaration", "class_declaration",
                          "interface_declaration", "enum_declaration", "record_declaration"):
            continue
        mods = next((x for x in m.children if x.type == "modifiers"), None)
        words = set(re.findall(rb"\w+", src[mods.start_byte:mods.end_byte])) if mods is not None else set()
        if b"public" in words or b"private" in words:
            continue
        if m.type == "field_declaration":
            for d in m.children:
                if d.type == "variable_declarator":
                    out.add(_txt(src, d.child_by_field_name("name")))
        elif m.type != "constructor_declaration":
            nm = m.child_by_field_name("name")
            if nm is not None:
                out.add(_txt(src, nm))
    return out


def _hidden_ctor(src: bytes, tnode) -> bool:
    """True when the class has a package-private or protected constructor (`new Type(...)` outside the package fails)."""
    body = tnode.child_by_field_name("body")
    for m in (body.children if body is not None else []):
        if m.type == "constructor_declaration":
            mods = next((x for x in m.children if x.type == "modifiers"), None)
            words = set(re.findall(rb"\w+", src[mods.start_byte:mods.end_byte])) if mods is not None else set()
            if b"public" not in words and b"private" not in words:
                return True
    return False


def _java_files(root: Path) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("."))
        out += [Path(dirpath) / f for f in sorted(filenames) if f.endswith(".java")]
    return out


def _add_imports(text: str, names: list, eol: str) -> str:
    """Add `import x;` lines after the last import (or after the package line)."""
    have = set(re.findall(r"(?m)^import\s+([\w.]+)\s*;", text))
    names = [n for n in names if n not in have]
    if not names:
        return text
    block = eol.join(f"import {n};" for n in names)
    ms = list(re.finditer(r"(?m)^import\s[^;]*;[^\S\r\n]*", text))
    if ms:
        e = ms[-1].end()
        return text[:e] + eol + block + text[e:]
    m = re.search(r"(?m)^package\s[^;]*;[^\S\r\n]*", text)
    if m:
        return text[:m.end()] + eol + eol + block + text[m.end():]
    return block + eol + eol + text


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: Java types move one at a time (each is its own file); pass one symbol."
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
    if path_b.suffix != ".java":
        return "move_symbol: to_file must be a .java file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."

    src_a, tree_a = _parse(path_a)
    tops = _tops(src_a, tree_a)
    name = node["label"].split(".")[-1]
    hit = next((t for t in tops if t[0] == name), None)
    if hit is None:
        return (f"move_symbol: {name} is not a top-level type of {rel_a} (nested types and members can't be moved on "
                f"their own); nothing was written.")
    if len(tops) > 1:
        return (f"move_symbol: {rel_a} declares {len(tops)} top-level types ({', '.join(t[0] for t in tops)}); "
                f"moving one of them out of the file is not supported. Nothing was written.")
    if path_a.stem != name:
        return f"move_symbol: {rel_a} is not named after {name}; nothing was written."
    if path_b.stem != name:
        return f"move_symbol: the new file must be named {name}.java (Java ties the file name to the type); nothing was written."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."
    if path_b.exists():
        return f"move_symbol: {path_b.name} already exists at the target; nothing was written."
    pkg_a = _package(src_a, tree_a)
    dirs = pkg_a.split(".") if pkg_a else []
    parts_a = list(path_a.parent.parts)
    if dirs and parts_a[len(parts_a) - len(dirs):] != dirs:
        return f"move_symbol: {rel_a} is not in the directory its package {pkg_a} names; nothing was written."
    src_root = Path(*parts_a[:len(parts_a) - len(dirs)]) if dirs else path_a.parent
    try:
        rel_dirs = path_b.parent.relative_to(src_root).parts
    except ValueError:
        return f"move_symbol: the target must be under the same source root ({src_root.name}); nothing was written."
    if not rel_dirs:
        return "move_symbol: the target would be in the default package; nothing was written."
    pkg_b = ".".join(rel_dirs)
    if pkg_b == pkg_a:
        return "move_symbol: the symbol is already in that package."
    old_fqn, new_fqn = f"{pkg_a}.{name}" if pkg_a else name, f"{pkg_b}.{name}"
    eol = "\r\n" if b"\r\n" in src_a else "\n"

    # what the type uses from its old package without an import
    used = _idents(src_a, hit[1])
    sibs = {}
    for p in _java_files(root):
        if p.parent == path_a.parent and p != path_a:
            sibs[p.stem] = p
    needs = []
    text_a0 = src_a.decode("utf-8", errors="replace")
    for nm in sorted(used - {name}):
        if nm in sibs:
            ssrc, stree = _parse(sibs[nm])
            top = next((t for t in _tops(ssrc, stree) if t[0] == nm), None)
            if top is None or not top[2]:
                return (f"move_symbol: {name} uses {nm}, which is package-private in {pkg_a}; moving it away would break "
                        f"that access. Nothing was written.")
            clash = _hidden_members(ssrc, top[1]) & (used - {nm})
            if clash:
                return (f"move_symbol: {name} may use package-private or protected members of {nm} "
                        f"({', '.join(sorted(clash)[:4])}); they are not reachable from {pkg_b}. Nothing was written.")
            uses_rx = re.compile(r"\b(extends|new)\s+" + re.escape(nm) + r"\b")
            if uses_rx.search(text_a0) and (_hidden_ctor(ssrc, top[1]) or (
                    re.search(r"\bextends\s+" + re.escape(nm) + r"\b", text_a0) and _hidden_members(ssrc, top[1]))):
                return (f"move_symbol: {name} extends or constructs {nm}, which has package-private or protected members "
                        f"or constructors that stay inside {pkg_a}; nothing was written.")
            if (path_b.parent / f"{nm}.java").exists():
                return f"move_symbol: {pkg_b} already has a type named {nm}, which {name} uses from {pkg_a}; nothing was written."
            if not re.search(r"(?m)^import\s+[\w.]*\." + re.escape(nm) + r"\s*;", src_a.decode("utf-8", "replace")):
                needs.append(f"{pkg_a}.{nm}")

    text_a = src_a.decode("utf-8", errors="surrogateescape")
    new_b = re.sub(r"(?m)^package\s+[\w.]+\s*;", f"package {pkg_b};", text_a, count=1)
    new_b = _add_imports(new_b, needs, eol)
    new_b = re.sub(r"\b" + re.escape(old_fqn) + r"\b", new_fqn, new_b)

    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")
    hidden = _hidden_members(src_a, hit[1])
    hidden_ctor = _hidden_ctor(src_a, hit[1]) and hit[1].type != "enum_declaration"
    extends_rx = re.compile(r"\bextends\s+" + re.escape(name) + r"\b")
    new_rx = re.compile(r"\bnew\s+" + re.escape(name) + r"\b\s*(?:<[^>(]*>)?\s*\(")
    # other files
    new_files: dict = {}
    rewritten, added = [], []
    fq_rx = re.compile(r"(?<![\w.])" + re.escape(old_fqn) + r"(?![\w])")
    for p in _java_files(root):
        if p == path_a:
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        txt = raw.decode("utf-8", errors="surrogateescape")
        if name not in txt:
            continue
        e = "\r\n" if "\r\n" in txt else "\n"
        try:
            sp, tp = _parse(p)
        except Exception:
            continue
        pk = _package(sp, tp)
        out = txt
        n_fq = len(fq_rx.findall(out))
        out = fq_rx.sub(new_fqn, out)
        uses = name in _idents(sp, tp)
        wild = re.search(r"(?m)^import\s+" + re.escape(pkg_a) + r"\.\*\s*;", txt) is not None if pkg_a else False
        has_new = re.search(r"(?m)^import\s+" + re.escape(new_fqn) + r"\s*;", out) is not None
        if pk == pkg_a and uses and hidden_ctor and extends_rx.search(txt):
            return (f"move_symbol: {rel(p)} (same package) extends {name}, which has a package-private or protected "
                    f"constructor; nothing was written.")
        if pk == pkg_a and uses and hidden_ctor and new_rx.search(txt):
            return (f"move_symbol: {rel(p)} (same package) calls a package-private or protected constructor of {name}; "
                    f"it would no longer be reachable from {pkg_a}. Nothing was written.")
        if pk == pkg_a and uses and hidden & (_idents(sp, tp) - {name}):
            return (f"move_symbol: {rel(p)} (same package) refers to {', '.join(sorted(hidden & _idents(sp, tp))[:4])}, "
                    f"which {name} declares package-private or protected; they would no longer be reachable. "
                    f"Nothing was written.")
        if pk == pkg_a and uses:
            out = _add_imports(out, [new_fqn], e)
            added.append(p)
        elif wild and uses and not has_new and pk != pkg_b:
            out = _add_imports(out, [new_fqn], e)
            added.append(p)
        if pk == pkg_b:                      # the import became redundant
            out = re.sub(r"(?m)^import\s+" + re.escape(new_fqn) + r"\s*;[^\S\r\n]*\r?\n", "", out)
        if out != txt:
            new_files[p.resolve()] = out.encode("utf-8", errors="surrogateescape")
            rewritten.append(p)

    new_files[path_b] = new_b.encode("utf-8", errors="surrogateescape")
    if apply:
        for p, data in new_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        path_a.unlink()
    verb = "moved" if apply else "would move"
    out = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
           f"{verb} {name} from {pkg_a or '(default)'} to {pkg_b}: {rel_a} -> {rel(path_b)}"]
    if needs:
        out.append("imports added to the moved type (it used these without an import): " + ", ".join(n.split(".")[-1] for n in needs))
    n_imp = len(rewritten) - len(added)
    if rewritten:
        out.append(f"files updated: {len(rewritten)} ({len(added)} gained an import of {name})")
        out += [f"  {rel(p)}" for p in rewritten[:12]]
        if len(rewritten) > 12:
            out.append(f"  ... {len(rewritten) - 12} more")
    out.append(f"Members of {name} that were package-private are no longer reachable from {pkg_a}: compile to confirm.")
    if apply:
        out += ["", "Done: the file, its package line and the imports are already changed. "
                    "Do NOT Read or Edit these files again to redo it.",
                "Next: check_change, then compile / run the related tests."]
    return "\n".join(out)
