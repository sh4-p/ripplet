"""move_symbol for C and C++: move a top-level function, type, macro, `using` alias or global variable to another file.

C/C++ share declarations through `#include`, so where a thing may go depends on what it is:

* a type (struct / union / enum / class / typedef / template), a macro or a `using` alias is shared by every file that includes
  its header, so it can only move to another header; the old file then `#include`s the new header where the thing stood, which
  keeps it visible to everyone who included the old file;
* a function with external linkage is a symbol the linker finds, so it moves to a source file (the prototype in its header
  stays valid); when the old file still calls it, a prototype is left where it stood; `static` / `inline` functions and
  `static` variables are file-local and move only to a place where nothing else needs them;
* the moved code gets the old file's `#include` lines (quoted paths re-based); it may use things of the old file only when that
  is safe: a header target can't depend on the old header (an include cycle), a source target includes an old header, and a
  source-to-source move re-declares the old file's external functions it calls.
It refuses code inside a namespace, `extern "C"` block or `#if`, member functions defined outside their class, and anything
that would leave a file-local thing behind. The new file must be added to the build by hand.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP = {".git", "build", "out", "node_modules", "third_party", "vendor", "cmake-build-debug", ".vs"}
_HDR = (".h", ".hpp", ".hh", ".hxx")
_SRC = (".c", ".cc", ".cpp", ".cxx")
_parsers: dict = {}


def _parser_for(path: Path):
    key = "c" if path.suffix.lower() == ".c" else "cpp"
    if key not in _parsers:
        import tree_sitter as ts
        if key == "c":
            import tree_sitter_c as m
        else:
            import tree_sitter_cpp as m
        _parsers[key] = ts.Parser(ts.Language(m.language()))
    return _parsers[key]


def _parse(src: bytes, path: Path):
    return _parser_for(path).parse(src).root_node


def _t(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="surrogateescape")


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _storage(src: bytes, n) -> set:
    words = set()
    for c in n.children:
        if c.type in ("storage_class_specifier", "type_qualifier"):
            words.add(_t(src, c).strip())
        if c.type in ("virtual_function_specifier", "explicit_function_specifier"):
            words.add(_t(src, c).strip())
    return words


def _fn_declarator(n):
    d = n.child_by_field_name("declarator")
    while d is not None and d.type in ("pointer_declarator", "reference_declarator", "parenthesized_declarator", "attributed_declarator"):
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in d.children if c.is_named and c.type not in ("type_qualifier",)), None)
        d = nxt
    return d


def _name_and_kind(src: bytes, n):
    """(name, kind, extra) kind: func | type | macro | var ; None when not movable / not a definition"""
    t = n.type
    if t in ("preproc_def", "preproc_function_def"):
        x = next((c for c in n.children if c.type == "identifier"), None)
        return (_t(src, x), "macro") if x is not None else None
    if t == "function_definition":
        d = _fn_declarator(n)
        if d is None or d.type != "function_declarator":
            return None
        inner = d.child_by_field_name("declarator")
        txt = _t(src, inner) if inner is not None else ""
        if "::" in txt:
            return (txt, "method")
        return (txt, "func")
    if t == "type_definition":
        d = n.child_by_field_name("declarator")
        return (_t(src, d), "type") if d is not None else None
    if t in ("struct_specifier", "union_specifier", "enum_specifier", "class_specifier"):
        nm = n.child_by_field_name("name")
        has_body = any(c.type in ("field_declaration_list", "enumerator_list") for c in n.children)
        return (_t(src, nm), "type") if nm is not None and has_body else None
    if t == "alias_declaration":
        nm = n.child_by_field_name("name")
        return (_t(src, nm), "type") if nm is not None else None
    if t == "template_declaration":
        inner = next((c for c in reversed(n.children) if c.is_named and c.type not in ("template_parameter_list", "comment")), None)
        if inner is None:
            return None
        r = _name_and_kind(src, inner)
        return (r[0], "type" if r[1] in ("func", "type") else r[1]) if r else None
    if t == "declaration":
        decls = [c for c in n.children if c.type in ("init_declarator", "identifier", "pointer_declarator", "array_declarator")]
        if len(decls) != 1:
            return None
        d = decls[0]
        if d.type == "init_declarator":
            d = d.child_by_field_name("declarator")
        while d is not None and d.type in ("pointer_declarator", "array_declarator", "parenthesized_declarator"):
            d = d.child_by_field_name("declarator")
        if d is None or d.type != "identifier":
            return None
        if "extern" in _storage(src, n) and not any(c.type == "init_declarator" for c in n.children):
            return None
        return (_t(src, d), "var")
    return None


def _tops(src: bytes, root) -> list:
    """(name, node, kind, end node incl. trailing ';')"""
    out = []
    kids = root.children
    for i, c in enumerate(kids):
        r = _name_and_kind(src, c)
        if not r:
            continue
        end = c
        if c.type in ("struct_specifier", "union_specifier", "enum_specifier", "class_specifier", "template_declaration") \
                and i + 1 < len(kids) and kids[i + 1].type == ";":
            end = kids[i + 1]
        out.append((r[0], c, r[1], end))
    return out


def _idents(src: bytes, n) -> set:
    return {_t(src, x) for x in _walk(n) if x.type in ("identifier", "type_identifier")}


def _doc_start(src: bytes, n) -> int:
    start, prev = n.start_byte, n.prev_sibling
    while prev is not None and prev.type == "comment" and src[prev.end_byte:start].count(b"\n") <= 1:
        start, prev = prev.start_byte, prev.prev_sibling
    return start


def _includes(src: bytes, root) -> list:
    return [c for c in root.children if c.type == "preproc_include"]


def _inc_path(src: bytes, n) -> str:
    x = next((c for c in n.children if c.type in ("string_literal", "system_lib_string")), None)
    return _t(src, x) if x is not None else ""


def _c_files(root: Path) -> list:
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in _SKIP and not d.startswith("."))
        out += [Path(dp) / f for f in sorted(fn) if f.lower().endswith(_HDR + _SRC)]
    return out


def _signature(src: bytes, fn) -> str:
    body = fn.child_by_field_name("body")
    end = body.start_byte if body is not None else fn.end_byte
    return re.sub(r"\s+", " ", src[fn.start_byte:end].decode("utf-8", errors="surrogateescape")).strip() + ";"


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: C/C++ declarations move one at a time; pass one symbol."
    node, _o = resolve(cx, symbol.strip())
    if node is None:
        where, _s, nm0 = symbol.strip().rpartition("::")
        if where.lower().endswith(_HDR + _SRC):
            node = {"file_path": where, "label": nm0}
    if node is None and "::" not in symbol:            # a typedef / struct is not always a graph node: look in the files
        found = []
        for r in cx.execute("SELECT DISTINCT file_path FROM nodes WHERE type IN ('file', 'module')").fetchall():
            fp = r["file_path"] or ""
            if not fp.lower().endswith(_HDR + _SRC):
                continue
            pa = file_of([str(x) for x in roots], fp)
            if pa is None:
                continue
            try:
                s_ = pa.read_bytes()
                if symbol.strip().encode() not in s_:
                    continue
                if any(t[0] == symbol.strip() for t in _tops(s_, _parse(s_, pa))):
                    found.append(fp)
            except OSError:
                continue
        if len(found) == 1:
            node = {"file_path": found[0], "label": symbol.strip()}
        elif len(found) > 1:
            return f"move_symbol: {symbol!r} is defined in several files ({', '.join(found[:4])}); name it as path::name."
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
    if path_b.suffix.lower() not in _HDR + _SRC:
        return "move_symbol: to_file must be a C/C++ header or source file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."
    name = re.split(r"::|\.", node["label"])[-1]
    src_a = path_a.read_bytes()
    root_a = _parse(src_a, path_a)
    tops = _tops(src_a, root_a)
    hits = [t for t in tops if t[0] == name]
    if not hits:
        nested = next((x for x in _walk(root_a) if x.type in ("function_definition", "struct_specifier", "class_specifier", "enum_specifier",
                                                                "preproc_def", "preproc_function_def", "type_definition")
                       and (_name_and_kind(src_a, x) or ("",))[0] == name), None)
        if nested is not None:
            return (f"move_symbol: {name} sits inside a namespace, `extern \"C\"` block, `#if` or another declaration of {rel_a}; "
                    f"that is not supported. Nothing was written.")
        return f"move_symbol: {name} is not a top-level function, type, macro, alias or variable defined in {rel_a}; nothing was written."
    if len(hits) > 1:
        return f"move_symbol: {name} is defined {len(hits)} times in {rel_a} (overloads or #if branches?); nothing was written."
    _n, decl, kind, endn = hits[0]
    if kind == "method":
        return f"move_symbol: {name} is a member function defined outside its class; it moves with the class. Nothing was written."
    a_hdr, b_hdr = path_a.suffix.lower() in _HDR, path_b.suffix.lower() in _HDR
    stor = _storage(src_a, decl) if decl.type in ("function_definition", "declaration") else set()
    local = "static" in stor or "inline" in stor or "constexpr" in stor or (decl.type == "template_declaration")
    is_ext_func = kind == "func" and not local
    # ---- where may it go
    if kind in ("type", "macro") and not b_hdr:
        return (f"move_symbol: {name} is shared through #include; it can only move to a header, not to {path_b.name}. "
                f"Nothing was written.")
    if kind == "func" and local and a_hdr and not b_hdr:
        return (f"move_symbol: {name} is static/inline in a header; every includer uses it, so it can only move to a header. "
                f"Nothing was written.")
    if is_ext_func and b_hdr:
        return (f"move_symbol: {name} has external linkage and is defined once; it can't move into a header "
                f"(multiple definitions). Nothing was written.")
    if kind == "var":
        if a_hdr and not b_hdr:
            return f"move_symbol: {name} is declared in a header; it can only move to a header. Nothing was written."
        if not a_hdr and b_hdr and "static" not in stor:
            return f"move_symbol: {name} is a global variable defined in a source file; a header would define it in every includer. Nothing was written."
    src_b0 = path_b.read_bytes() if path_b.exists() else None
    root_b = _parse(src_b0, path_b) if src_b0 is not None else None
    if root_b is not None and any(t[0] == name for t in _tops(src_b0, root_b)):
        return f"move_symbol: {path_b.name} already defines {name}; nothing was written."
    eol = b"\r\n" if b"\r\n" in src_a else b"\n"
    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")

    # ---- the cut
    start = _doc_start(src_a, decl)
    end = endn.end_byte
    chunk = src_a[start:end]
    cut_start = src_a.rfind(b"\n", 0, start) + 1
    cut_end = end
    while src_a[cut_end:cut_end + 1] in (b" ", bytes([9])):
        cut_end += 1
    if src_a[cut_end:cut_end + 2] == b"\r\n":
        cut_end += 2
    elif src_a[cut_end:cut_end + 1] == b"\n":
        cut_end += 1
    rest_probe = src_a[:cut_start] + src_a[cut_end:]
    rest_root = _parse(rest_probe, path_a)
    rest_ids = set()
    for c in rest_root.children:
        if c.type != "preproc_include":
            rest_ids |= _idents(rest_probe, c)
    for c in rest_root.children:                      # macros used by name in the remaining code
        if c.type in ("preproc_def", "preproc_function_def"):
            pass
    rest_words = set(re.findall(rb"[A-Za-z_]\w*", rest_probe))
    uses_in_a = name.encode() in rest_words
    if local and not b_hdr and uses_in_a and kind in ("func", "var"):
        return (f"move_symbol: {name} is file-local (static/inline) and {rel_a} still uses it; moving it to {path_b.name} would "
                f"hide it. Nothing was written.")

    # ---- what the moved code needs from the old file
    used = _idents(src_a, decl) | {w.decode() for w in re.findall(rb"[A-Za-z_]\w*", src_a[decl.start_byte:endn.end_byte])}
    others = {t[0]: t for t in tops if t[0] != name}
    protos = []
    for c in root_a.children:                           # prototypes of A's own file that the moved code relies on
        if c.type == "declaration" and "static" not in _storage(src_a, c):
            d = next((x for x in c.children if x.type == "function_declarator"), None)
            if d is None:
                d = next((x for x in c.children if x.type == "pointer_declarator"), None)
                d = _fn_declarator(c) if d is not None else None
            if d is not None and d.type == "function_declarator":
                pn = d.child_by_field_name("declarator")
                pname = _t(src_a, pn) if pn is not None else ""
                if pname in used and pname not in others and pname != name:
                    protos.append(re.sub(r"\s+", " ", _t(src_a, c)).strip())
    for nm in sorted(used & set(others)):
        _nm, onode, okind, _e = others[nm]
        ostor = _storage(src_a, onode) if onode.type in ("function_definition", "declaration") else set()
        olocal = "static" in ostor or "inline" in ostor
        if a_hdr and b_hdr:
            return (f"move_symbol: {name} uses {nm} of {rel_a}; the new header would have to include the old one, which includes "
                    f"it back (an include cycle). Nothing was written.")
        if a_hdr and not b_hdr:
            continue                                  # the source includes the old header
        if okind == "func" and not olocal and not b_hdr:
            protos.append(_signature(src_a, onode))
            continue
        return (f"move_symbol: {name} uses {nm}, which is private to {rel_a} ({'static' if olocal else okind}); "
                f"nothing was written.")

    # ---- includes
    def rebase(inc_node) -> str:
        line = _t(src_a, inc_node).strip()
        p = _inc_path(src_a, inc_node)
        if p.startswith('"'):
            tgt = (path_a.parent / p.strip('"')).resolve()
            if tgt.exists():
                r = os.path.relpath(tgt, path_b.parent).replace("\\", "/")
                return f'#include "{r}"'
        return line
    carried = [rebase(n) for n in _includes(src_a, root_a)]
    if a_hdr and not b_hdr:
        carried.append(f'#include "{os.path.relpath(path_a, path_b.parent).replace(chr(92), "/")}"')
    carried = list(dict.fromkeys(carried))

    # ---- the old file
    repl = b""
    if kind in ("type", "macro") or (local and b_hdr) or (kind == "var" and b_hdr):
        inc_to_b = os.path.relpath(path_b, path_a.parent).replace("\\", "/")
        repl = f'#include "{inc_to_b}"'.encode() + eol
    elif is_ext_func and uses_in_a:
        repl = _signature(src_a, decl).encode() + eol
    new_a = src_a[:cut_start] + repl + src_a[cut_end:]

    # ---- the target
    if src_b0 is None:
        head = b""
        if b_hdr:
            head = b"#pragma once" + eol + eol
        incs = (eol.join(c.encode() for c in carried) + eol + eol) if carried else b""
        pr = (eol.join(p_.encode() for p_ in protos) + eol + eol) if protos else b""
        text_b = head + incs + pr + chunk.rstrip(b"\r\n") + eol
    else:
        base = src_b0
        have = {_t(src_b0, n).strip() for n in _includes(src_b0, root_b)}
        add = [c for c in carried if c not in have]
        pr = [p_ for p_ in protos if p_.encode() not in base]
        ins = eol.join(x.encode() for x in add + pr)
        incs_b = _includes(src_b0, root_b)
        if ins:
            if incs_b:
                pos = incs_b[-1].end_byte
                base = base[:pos] + eol + ins + base[pos:]
            else:
                base = ins + eol + eol + base
        text_b = base.rstrip(b"\r\n") + eol + eol + chunk.rstrip(b"\r\n") + eol
    out_files = {path_b: text_b, path_a: new_a}

    if apply:
        for p, data in out_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    verb = "moved" if apply else "would move"
    lines = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
             f"{verb} {kind} {name}: {rel_a} -> {rel(path_b)}"]
    if repl.startswith(b"#include"):
        lines.append(f"{rel_a} now includes {rel(path_b)} where {name} stood, so everything that included it still sees {name}.")
    elif repl:
        lines.append(f"{rel_a} keeps a prototype of {name} where it stood (it still calls it).")
    if protos:
        lines.append("prototypes added to the moved file for the old file's functions it calls: " + ", ".join(p_.split("(")[0].split()[-1] for p_ in protos))
    if not b_hdr and src_b0 is None:
        lines.append(f"Add {rel(path_b)} to the build (Makefile / CMake / project file): the linker needs it.")
    lines.append("Macros and #if-guarded variants of the same name, and non-C files, were not checked.")
    if apply:
        lines += ["", "Done: the definition and the includes are already changed. Do NOT Read or Edit these files again to redo it.",
                  "Next: build and run the tests."]
    return "\n".join(lines)
