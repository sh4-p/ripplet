"""move_symbol for C#: move a top-level class, interface, struct, enum, record or delegate to another file.

A C# file may hold several types and a namespace is a name, not a folder, so a move is two separate things:

* the declaration (attributes and doc comments included) leaves its file and lands in the target file, with the
  `using` directives it may need; an existing target keeps its own namespace, a new file gets the namespace that the
  project's folder convention gives it (the old namespace with the folder part swapped), or the old namespace when the
  old one does not follow the folders;
* when the namespace changes, files that used the type through the old namespace (declared inside it, or
  `using Old;`) gain `using New;`, fully qualified names are rewritten, the moved type gains `using Old;` for the
  siblings it used, and the old file is deleted if nothing is left in it.
It refuses partial types, a target in another project (different .csproj), a name the target namespace already has,
and a target file whose namespaces are ambiguous. Writes nothing in those cases.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP = {"node_modules", ".git", "bin", "obj", ".vs", "packages", ".idea"}
_TYPES = ("class_declaration", "interface_declaration", "struct_declaration", "enum_declaration",
          "record_declaration", "record_struct_declaration", "delegate_declaration")
_parser = None


def _parse_src(src: bytes):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_c_sharp as tc
        _parser = ts.Parser(ts.Language(tc.language()))
    return _parser.parse(src).root_node


def _parse(path: Path):
    src = path.read_bytes()
    return src, _parse_src(src)


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _txt(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="surrogateescape")


def _idents(src: bytes, node) -> set:
    return {_txt(src, x) for x in _walk(node) if x.type == "identifier"}


def _cs_files(root: Path) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("."))
        out += [Path(dirpath) / f for f in sorted(filenames) if f.endswith(".cs")]
    return out


def _decls(src: bytes, root) -> list:
    """(namespace, node, scope) for each type declared directly in a namespace (or the global one)."""
    out = []

    def visit(children, ns, scope):
        file_ns = ns
        for c in children:
            if c.type == "file_scoped_namespace_declaration":
                nm = next((x for x in c.children if x.type in ("identifier", "qualified_name")), None)
                file_ns = _txt(src, nm) if nm is not None else ""
                scope = ("file", c)
            elif c.type == "namespace_declaration":
                nm = c.child_by_field_name("name") or next((x for x in c.children if x.type in ("identifier", "qualified_name")), None)
                full = (ns + "." if ns else "") + _txt(src, nm)
                body = next((x for x in c.children if x.type == "declaration_list"), None)
                if body is not None:
                    visit(body.children, full, ("block", c, body))
            elif c.type in _TYPES:
                out.append((file_ns, c, scope))

    visit(root.children, "", ("global", None))
    return out


def _name(src: bytes, node) -> str:
    nm = node.child_by_field_name("name")
    return _txt(src, nm) if nm is not None else ""


def _namespaces(src: bytes, root) -> set:
    out = set()

    def visit(children, ns):
        for c in children:
            if c.type == "file_scoped_namespace_declaration":
                nm = next((x for x in c.children if x.type in ("identifier", "qualified_name")), None)
                if nm is not None:
                    out.add(_txt(src, nm))
            elif c.type == "namespace_declaration":
                nm = next((x for x in c.children if x.type in ("identifier", "qualified_name")), None)
                full = (ns + "." if ns else "") + _txt(src, nm)
                out.add(full)
                body = next((x for x in c.children if x.type == "declaration_list"), None)
                if body is not None:
                    visit(body.children, full)

    visit(root.children, "")
    return out


def _usings(src: bytes, root) -> list:
    return [(c, re.sub(r"\s+", " ", _txt(src, c)).strip()) for c in root.children if c.type == "using_directive"]


def _plain_using(text: str) -> str | None:
    m = re.fullmatch(r"using ([\w.]+);", text)
    return m.group(1) if m else None


def _span(src: bytes, node):
    """Byte range of a declaration with the comments directly above it, widened to whole lines."""
    start = node.start_byte
    prev = node.prev_sibling
    while prev is not None and prev.type == "comment":
        if src[prev.end_byte:start].count(b"\n") > 1:
            break
        before = prev.prev_sibling
        if before is not None and before.end_point[0] == prev.start_point[0] and before.type != "comment":
            break
        start = prev.start_byte
        prev = prev.prev_sibling
    s = src.rfind(b"\n", 0, start) + 1
    if src[s:start].strip():
        s = start
    e = node.end_byte
    k = e
    while k < len(src) and src[k:k + 1] in (b" ", b"\t", b"\r"):
        k += 1
    if k >= len(src) or src[k:k + 1] == b"\n":
        e = k + 1 if k < len(src) else k
    return s, e


def _proj_dir(d: Path):
    for d in [d, *d.parents]:
        if any(d.glob("*.csproj")):
            return d
    return None


def _insert_using(text: str, src: bytes, root, names: list, eol: str) -> str:
    have = {_plain_using(t) for _c, t in _usings(src, root)}
    names = [n for n in names if n and n not in have]
    if not names:
        return text
    block = eol.join(f"using {n};" for n in names)
    us = [c for c, _t in _usings(src, root)]
    if us:
        e = us[-1].end_byte
        b = text.encode("utf-8", errors="surrogateescape")
        return (b[:e] + (eol + block).encode() + b[e:]).decode("utf-8", errors="surrogateescape")
    first = next((c for c in root.children if c.type != "comment"), None)
    b = text.encode("utf-8", errors="surrogateescape")
    at = first.start_byte if first is not None else len(b)
    return (b[:at] + (block + eol + eol).encode() + b[at:]).decode("utf-8", errors="surrogateescape")


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: C# types move one at a time; pass one symbol."
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
    if path_b.suffix != ".cs":
        return "move_symbol: to_file must be a .cs file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."

    src_a, tree_a = _parse(path_a)
    name = node["label"].split(".")[-1]
    decls = _decls(src_a, tree_a)
    hit = next(((ns, n, sc) for ns, n, sc in decls if _name(src_a, n) == name), None)
    if hit is None:
        return (f"move_symbol: {name} is not a top-level type of {rel_a} (nested types and members can't be moved on "
                f"their own); nothing was written.")
    ns_a, tnode, scope_a = hit
    if any(c.type == "modifier" and _txt(src_a, c) == "partial" for c in tnode.children):
        return f"move_symbol: {name} is a partial type (its parts live in several files); nothing was written."
    if tree_a.has_error and any(c.type == "ERROR" for c in tree_a.children):
        return f"move_symbol: the C# parser can't read {rel_a} reliably; nothing was written."
    pd_a = _proj_dir(path_a.parent)
    pd_b = next((d for d in [path_b.parent, *path_b.parent.parents] if d.exists() and any(d.glob("*.csproj"))), None)
    if pd_a is None or pd_a != pd_b:
        return "move_symbol: the target must be in the same project (.csproj) as the type; nothing was written."
    eol = "\r\n" if b"\r\n" in src_a else "\n"

    # target file and namespace
    exists_b = path_b.is_file()
    if exists_b:
        src_b, tree_b = _parse(path_b)
        nss = _namespaces(src_b, tree_b)
        decls_b = _decls(src_b, tree_b)
        if len(nss) > 1:
            return f"move_symbol: {path_b.name} declares several namespaces; pick a target with one. Nothing was written."
        ns_b = next(iter(nss), "")
        if any(_name(src_b, n) == name and ns == ns_b for ns, n, _s in decls_b):
            return f"move_symbol: {path_b.name} already declares {name}; nothing was written."
        style_b = decls_b[0][2] if decls_b else None
    else:
        src_b, tree_b = b"", None
        rel_a_parts = list(path_a.parent.relative_to(pd_a).parts)
        rel_b_parts = list(path_b.parent.relative_to(pd_a).parts)
        parts = ns_a.split(".") if ns_a else []
        if rel_a_parts and [p.lower() for p in parts[len(parts) - len(rel_a_parts):]] == [p.lower() for p in rel_a_parts]:
            ns_b = ".".join(parts[:len(parts) - len(rel_a_parts)] + rel_b_parts)
        else:
            ns_b = ns_a
        style_b = None
    # a type with this name already visible in the target namespace
    for p in _cs_files(pd_a):
        if p == path_a:
            continue
        try:
            sp, tp = _parse(p)
        except Exception:
            continue
        if any(ns == ns_b and _name(sp, n) == name for ns, n, _s in _decls(sp, tp)) and p != path_b:
            return f"move_symbol: namespace {ns_b} already has a type named {name} ({p.name}); nothing was written."

    # the declaration text
    s, e = _span(src_a, tnode)
    seg = src_a[s:e].decode("utf-8", errors="surrogateescape").rstrip("\r\n")
    lines = seg.replace("\r\n", "\n").split("\n")
    first_line = lines[0]
    ia = first_line[:len(first_line) - len(first_line.lstrip())]
    if exists_b and style_b is not None:
        if style_b[0] == "block":
            body = style_b[2]
            m = next((c for c in body.children if c.type in _TYPES), None)
            ib = (src_b.rfind(b"\n", 0, m.start_byte) + 1 and
                  src_b[src_b.rfind(b"\n", 0, m.start_byte) + 1:m.start_byte].decode()) if m is not None else "    "
            ib = ib if ib.strip() == "" else "    "
        else:
            ib = ""
    elif exists_b:
        ib = ""
    else:
        ib = ia if scope_a[0] == "block" else ""
    new_lines = [(ib + l[len(ia):]) if l.startswith(ia) else l for l in lines]
    seg_b = eol.join(new_lines)

    # usings the declaration may need
    us_a = _usings(src_a, tree_a)
    have_b = {t for _c, t in _usings(src_b, tree_b)} if exists_b else set()
    carried = [t for _c, t in us_a if t not in have_b and not t.startswith("global ")]
    need_ns = set()
    if ns_a != ns_b:
        used = _idents(src_a, tnode) - {name}
        for p in _cs_files(pd_a):
            try:
                sp, tp = _parse(p)
            except Exception:
                continue
            for ns, n, _s in _decls(sp, tp):
                # a type of the old namespace or of one of its parents is visible without a using
                if ns and (ns == ns_a or ns_a.startswith(ns + ".")) and _name(sp, n) in used                         and not (ns_b == ns or ns_b.startswith(ns + ".")):
                    need_ns.add(ns)
        for ns in sorted(need_ns):
            tok = f"using {ns};"
            if tok not in have_b and tok not in carried:
                carried.append(tok)
    text_b = ""
    if exists_b:
        b = src_b
        out_b = b
        # insert the declaration
        if style_b is not None and style_b[0] == "block":
            close = style_b[2].end_byte - 1
            pre = out_b[:close].rstrip()
            out_b = pre + (eol * 2).encode() + seg_b.encode() + eol.encode() + out_b[close:].lstrip(b" \t")
        else:
            out_b = out_b.rstrip() + (eol * 2).encode() + seg_b.encode() + eol.encode()
        text_b = out_b.decode("utf-8", errors="surrogateescape")
        tb = _parse_src(out_b)
        for t in reversed(carried):
            name_u = _plain_using(t)
            if name_u:
                text_b = _insert_using(text_b, text_b.encode("utf-8", errors="surrogateescape"), _parse_src(text_b.encode("utf-8", errors="surrogateescape")), [name_u], eol)
            else:
                ub = text_b.encode("utf-8", errors="surrogateescape")
                rb = _parse_src(ub)
                us = [c for c, _t in _usings(ub, rb)]
                at = us[-1].end_byte if us else 0
                text_b = (ub[:at] + ((eol if us else "") + t + (eol if not us else "")).encode() + ub[at:]).decode("utf-8", errors="surrogateescape")
    else:
        head = eol.join(carried)
        if scope_a[0] == "block" or scope_a[0] == "global":
            ns_open = f"namespace {ns_b}{eol}{{{eol}" if ns_b else ""
            body = (eol.join(("    " + l) if l.strip() else l for l in seg_b.split(eol))) if ns_b else seg_b
            text_b = (head + (eol * 2 if head else "") + ns_open + body + eol + ("}" + eol if ns_b else ""))
        else:
            text_b = (head + (eol * 2 if head else "") + (f"namespace {ns_b};{eol}{eol}" if ns_b else "") + seg_b + eol)

    # file A without the declaration
    new_a = src_a[:s] + src_a[e:]
    remaining = [d for d in decls if d[1] is not tnode]
    delete_a = not remaining
    text_a = new_a.decode("utf-8", errors="surrogateescape")
    text_a = re.sub(r"(\r?\n){4,}", eol * 3, text_a)

    new_files: dict = {}
    updated = []
    if ns_a != ns_b:
        fq_rx = re.compile(r"(?<![\w.])" + re.escape(f"{ns_a}.{name}" if ns_a else name) + r"(?![\w])") if ns_a else None
        new_fq = f"{ns_b}.{name}" if ns_b else name
        # extension methods are found through the namespace, so a call of one counts as a use of the type
        ext_names = set()
        for m in _walk(tnode):
            if m.type == "method_declaration":
                pl = m.child_by_field_name("parameters")
                first = next((c for c in (pl.children if pl is not None else []) if c.type == "parameter"), None)
                if first is not None and any(c.type == "modifier" and _txt(src_a, c) == "this" for c in first.children):
                    ext_names.add(_name(src_a, m))
        names_hit = {name} | ext_names
        # is the old namespace empty once the type has left?
        ns_empty = bool(ns_a)
        if ns_a:
            for p in _cs_files(pd_a):
                try:
                    sp, tp = _parse(p)
                except Exception:
                    continue
                for ns, n, _s in _decls(sp, tp):
                    if n is not tnode and (ns == ns_a or ns.startswith(ns_a + ".")) and not (p == path_a and _name(sp, n) == name):
                        ns_empty = False
                        break
                if not ns_empty:
                    break
        using_old_rx = re.compile(r"(?m)^[ \t]*using\s+" + re.escape(ns_a) + r"\s*;[ \t]*\r?\n") if ns_empty else None
        # file A keeps using the type
        ra = _parse_src(text_a.encode("utf-8", errors="surrogateescape"))
        if not delete_a and (names_hit & _idents(text_a.encode("utf-8", errors="surrogateescape"), ra)) and ns_b:
            text_a = _insert_using(text_a, text_a.encode("utf-8", errors="surrogateescape"), ra, [ns_b], eol)
            updated.append(path_a)
        for p in _cs_files(root):
            if p == path_a or p == path_b:
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            t = raw.decode("utf-8", errors="surrogateescape")
            if not any(n in t for n in names_hit) and not (using_old_rx is not None and using_old_rx.search(t)):
                continue
            try:
                sp = raw
                tp = _parse_src(raw)
            except Exception:
                continue
            e2 = "\r\n" if "\r\n" in t else "\n"
            out = t
            if fq_rx is not None:
                out = fq_rx.sub(new_fq, out)
            if names_hit & _idents(sp, tp):
                nss = _namespaces(sp, tp)
                us = {_plain_using(tx) for _c, tx in _usings(sp, tp)}
                sees_old = (ns_a in us) or any(n == ns_a or n.startswith(ns_a + ".") for n in nss) if ns_a else True
                sees_new = (ns_b in us) or any(n == ns_b or n.startswith(ns_b + ".") for n in nss) if ns_b else True
                if sees_old and not sees_new and ns_b:
                    out = _insert_using(out, out.encode("utf-8", errors="surrogateescape"),
                                        _parse_src(out.encode("utf-8", errors="surrogateescape")), [ns_b], e2)
            if using_old_rx is not None:
                out = using_old_rx.sub("", out)
            if out != t:
                new_files[p.resolve()] = out.encode("utf-8", errors="surrogateescape")
                updated.append(p)
        # Razor pages and components name the type in markup (`@model PrivacyModel`), resolved by their own namespace
        if ns_b:
            word_rx = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in sorted(names_hit)) + r")\b")
            razor_old_rx = re.compile(r"(?m)^(﻿?)@using\s+" + re.escape(ns_a) + r"[ \t]*(?:\r?\n|\Z)") if ns_empty else None
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP and not d.startswith(".")]
                for f in filenames:
                    if not f.endswith((".cshtml", ".razor")):
                        continue
                    p = Path(dirpath) / f
                    t = p.read_bytes().decode("utf-8", errors="surrogateescape")
                    out = t
                    if word_rx.search(t) and not re.search(r"(?m)^@using\s+" + re.escape(ns_b) + r"\s*$", t):
                        e2 = "\r\n" if "\r\n" in t else "\n"
                        bom = "﻿" if t.startswith("﻿") else ""
                        body = t[len(bom):]
                        ms = list(re.finditer(r"(?m)^@using\s[^\r\n]*", body))
                        body = (body[:ms[-1].end()] + e2 + f"@using {ns_b}" + body[ms[-1].end():]) if ms else f"@using {ns_b}{e2}" + body
                        out = bom + body
                    if fq_rx is not None:
                        out = fq_rx.sub(new_fq, out)
                    if razor_old_rx is not None:
                        out = razor_old_rx.sub(r"\1", out)
                    if out != t:
                        new_files[p.resolve()] = out.encode("utf-8", errors="surrogateescape")
                        updated.append(p)
    new_files[path_b] = text_b.encode("utf-8", errors="surrogateescape")
    if not delete_a:
        new_files[path_a] = text_a.encode("utf-8", errors="surrogateescape")

    if apply:
        for p, data in new_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        if delete_a:
            path_a.unlink()
    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")
    verb = "moved" if apply else "would move"
    out = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
           f"{verb} {name} from {rel_a} to {rel(path_b)}" + ("" if exists_b else " (new file)")
           + (f"; namespace {ns_a or '(global)'} -> {ns_b or '(global)'}" if ns_a != ns_b else f"; namespace {ns_a or '(global)'} kept")]
    if carried:
        out.append("usings added to the target: " + "; ".join(carried))
    if delete_a:
        out.append(f"{rel_a} had nothing else and was deleted.")
    if updated:
        out.append(f"files updated: {len(updated)} (gained `using {ns_b};` or a qualified name rewritten)")
        out += [f"  {rel(p)}" for p in updated[:12]]
        if len(updated) > 12:
            out.append(f"  ... {len(updated) - 12} more")
    if apply:
        out += ["", "Done: the declaration, usings and references are already changed. "
                    "Do NOT Read or Edit these files again to redo it.",
                "Next: check_change, then build / run the related tests."]
    return "\n".join(out)
