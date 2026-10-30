"""move_symbol for GDScript: move a `static func`, `const`, `enum` or inner `class` of a script to another script.

A script is a class. Other scripts reach its members as `Name.member`, where `Name` is its `class_name` or a constant bound
to `preload("res://x.gd")`. So moving `static func f` from `a.gd` to `b.gd` means:

* the member goes to the end of `b.gd` (a new file starts with `extends RefCounted`); consts it uses that are `preload` / `load`
  lines are copied; other members of `a.gd` it names (consts, enums, static funcs, inner classes) are written `A.member`, with
  `const A = preload("res://a.gd")` added to `b.gd` when `a.gd` has no `class_name`;
* every `A.f` (A: the class_name or a preload constant of the old script; also `preload("res://a.gd").f`) becomes `B.f`, with
  `const B = preload("res://b.gd")` added to the file when `b.gd` has no `class_name`; bare uses of `f` inside `a.gd` too.
It refuses instance functions and variables (they belong to an object), instance members of the old script the moved code uses,
a name the target already has, and a `preload` cycle (two scripts preloading each other is an error in Godot). Strings,
`call("f")` and `Callable` lookups are not rewritten.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP = {".git", ".godot", "addons", "node_modules", "build"}
_parser = None


def _parse(src: bytes):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_gdscript as tg
        _parser = ts.Parser(ts.Language(tg.language()))
    return _parser.parse(src).root_node


def _t(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="surrogateescape")


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _gd_files(root: Path) -> list:
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in _SKIP and not d.startswith("."))
        out += [Path(dp) / f for f in sorted(fn) if f.endswith(".gd")]
    return out


def _apply(src: bytes, edits: list) -> bytes:
    out, pos = [], 0
    for s, e, r in sorted(edits, key=lambda x: (x[0], x[1])):
        if s < pos:
            continue
        out.append(src[pos:s])
        out.append(r)
        pos = e
    out.append(src[pos:])
    return b"".join(out)


def _line_start(src: bytes, pos: int) -> int:
    return src.rfind(b"\n", 0, pos) + 1


def _name(src: bytes, n):
    x = next((c for c in n.children if c.type == "name"), None)
    return _t(src, x) if x is not None else None


def _members(src: bytes, root) -> list:
    """(name, node, kind) kind: static | func | const | enum | class | var"""
    out = []
    for c in root.children:
        if c.type == "function_definition":
            out.append((_name(src, c), c, "static" if any(x.type == "static_keyword" for x in c.children) else "func"))
        elif c.type == "const_statement":
            out.append((_name(src, c), c, "const"))
        elif c.type == "enum_definition":
            out.append((_name(src, c), c, "enum"))
        elif c.type == "class_definition":
            out.append((_name(src, c), c, "class"))
        elif c.type == "variable_statement":
            out.append((_name(src, c), c, "var"))
    return [m for m in out if m[0]]


def _class_name(src: bytes, root):
    n = next((c for c in root.children if c.type == "class_name_statement"), None)
    return _name(src, n) if n is not None else None


def _res(path: Path, proj: Path) -> str:
    return "res://" + path.relative_to(proj).as_posix()


def _pascal(stem: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[_\W]+", stem) if w) or "Moved"


def _preload_consts(src: bytes, root) -> dict:
    """{const name: res path} for `const X = preload("res://...")` / load(...)"""
    out = {}
    for c in root.children:
        if c.type == "const_statement":
            m = re.search(r"(?:preload|load)\s*\(\s*([\"'])(res://[^\"']+)\1\s*\)", _t(src, c))
            if m:
                out[_name(src, c)] = m.group(2)
    return out


def _in_str_or_comment(root, pos: int) -> bool:
    n = root.descendant_for_byte_range(pos, pos + 1)
    while n is not None:
        if n.type in ("string", "comment"):
            return True
        n = n.parent
    return False


def _refs(src: bytes, root, aliases: set, member: str) -> list:
    """identifier nodes of `X` in `X.member` (X in aliases)"""
    out = []
    for x in _walk(root):
        if x.type == "identifier" and _t(src, x) in aliases:
            p = x.parent
            if p is None or p.type != "attribute":
                continue
            kids = p.children
            i = next((k for k, c in enumerate(kids) if c.start_byte == x.start_byte and c.end_byte == x.end_byte), None)
            if i is None or i + 2 >= len(kids) + 0 or kids[i + 1].type != ".":
                continue
            nxt = kids[i + 2]
            nm = _t(src, nxt) if nxt.type == "identifier" else (_t(src, nxt.children[0]) if nxt.type == "attribute_call" and nxt.children else None)
            if nm == member:
                out.append(x)
    return out


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: GDScript members move one at a time; pass one symbol."
    node, _o = resolve(cx, symbol.strip())
    if node is None:
        where, _s, nm0 = symbol.strip().rpartition("::")
        if where.endswith(".gd"):
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
    if path_b.suffix != ".gd":
        return "move_symbol: to_file must be a .gd file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."
    proj = next((d for d in [path_a.parent, *path_a.parents] if (d / "project.godot").exists()), root)
    name = re.split(r"[.:]", node["label"])[-1]

    src_a = path_a.read_bytes()
    root_a = _parse(src_a)
    mem_a = _members(src_a, root_a)
    hits = [m for m in mem_a if m[0] == name]
    if not hits:
        return f"move_symbol: {name} is not a top-level static func, const, enum or class of {rel_a}; nothing was written."
    if len(hits) > 1:
        return f"move_symbol: {name} is declared {len(hits)} times in {rel_a}; nothing was written."
    _n, decl, kind = hits[0]
    if kind in ("func", "var"):
        return (f"move_symbol: {name} is an instance {'function' if kind == 'func' else 'variable'}; it belongs to an object of "
                f"{path_a.name}, so it can't be moved to another script. Nothing was written.")
    cls_a = _class_name(src_a, root_a)
    ref_a_global = cls_a
    consts_a = _preload_consts(src_a, root_a)
    src_b0 = path_b.read_bytes() if path_b.exists() else None
    root_b = _parse(src_b0) if src_b0 is not None else None
    if root_b is not None and any(m[0] == name for m in _members(src_b0, root_b)):
        return f"move_symbol: {path_b.name} already declares {name}; nothing was written."
    cls_b = _class_name(src_b0, root_b) if root_b is not None else None
    res_a, res_b = _res(path_a, proj), _res(path_b, proj)
    eol = b"\r\n" if b"\r\n" in src_a else b"\n"
    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")

    if res_b.encode() in src_a[decl.start_byte:decl.end_byte]:
        return f"move_symbol: {name} preloads {path_b.name}; moved there it would preload its own script. Nothing was written."

    # ---- what the moved code uses of its old script
    d_locals = set()
    for x in _walk(decl):
        if x.type == "name" and x.parent is not None and x.parent.type in ("variable_statement", "const_statement") and x != decl.children[0]:
            if x.parent != decl:
                d_locals.add(_t(src_a, x))
        if x.type == "identifier" and x.parent is not None:
            if x.parent.type in ("parameters", "typed_parameter", "default_parameter", "typed_default_parameter"):
                if x.parent.children[0] == x or x.parent.type == "parameters":
                    d_locals.add(_t(src_a, x))
            elif x.parent.type == "for_statement" and x.prev_sibling is not None and x.prev_sibling.type == "for":
                d_locals.add(_t(src_a, x))
    used = {}
    for x in _walk(decl):
        if x.type == "identifier":
            pv = x.prev_sibling
            if pv is not None and pv.type == ".":
                continue
            if x.parent is not None and x.parent.type == "type":
                continue
            used.setdefault(_t(src_a, x), []).append(x)
    members_a = {m[0]: m for m in mem_a if m[0] != name}
    carry, qualify = [], {}
    for nm, nodes in used.items():
        if nm in d_locals or nm not in members_a:
            continue
        _m, mnode, mk = members_a[nm]
        if mk == "const" and nm in consts_a:
            carry.append(_t(src_a, mnode))
        elif mk in ("static", "const", "enum", "class"):
            qualify[nm] = nodes
        else:
            return (f"move_symbol: {name} uses {nm}, an instance {'function' if mk == 'func' else 'variable'} of {path_a.name}; "
                    f"nothing was written.")

    # ---- reference names
    def ref_for_b_in(src, rt, own_path):
        """(name to write for the new script in this file, line to add or None)"""
        if cls_b:
            return cls_b, None
        existing = _preload_consts(src, rt)
        for c_name, rp in existing.items():
            if rp == res_b:
                return c_name, None
        nm = _pascal(path_b.stem)
        taken = {m[0] for m in _members(src, rt)}
        while nm in taken:
            nm += "Script"
        return nm, f'const {nm} = preload("{res_b}")'

    ref_a_in_b = ref_a_global
    add_b_lines = list(dict.fromkeys(carry))
    if qualify:
        if ref_a_in_b is None:
            nm_a = _pascal(path_a.stem)
            if root_b is not None:
                for c_name, rp in _preload_consts(src_b0, root_b).items():
                    if rp == res_a:
                        nm_a = c_name
                        break
                else:
                    add_b_lines.append(f'const {nm_a} = preload("{res_a}")')
            else:
                add_b_lines.append(f'const {nm_a} = preload("{res_a}")')
            ref_a_in_b = nm_a

    # ---- the moved text
    d_edits = []
    for nm, nodes in qualify.items():
        for x in nodes:
            d_edits.append((x.start_byte - decl.start_byte, x.end_byte - decl.start_byte, f"{ref_a_in_b}.{nm}".encode()))
    moved = _apply(src_a[decl.start_byte:decl.end_byte], d_edits)
    start, prev = decl.start_byte, decl.prev_sibling
    while prev is not None and prev.type == "comment" and src_a[prev.end_byte:start].count(b"\n") <= 1:
        start, prev = prev.start_byte, prev.prev_sibling
    moved = src_a[start:decl.start_byte] + moved

    # ---- files that reach the old script
    files = {p: (src_a if p == path_a else p.read_bytes()) for p in _gd_files(root)}
    out_files, updated = {}, []
    a_preloads_b = False
    word = re.compile(rb"\b" + re.escape(name.encode()) + rb"\b")
    for p, s in files.items():
        if p == path_b and src_b0 is None:
            continue
        s_cur = s
        rt = _parse(s_cur)
        aliases = {n_ for n_, rp in _preload_consts(s_cur, rt).items() if rp == res_a}
        if cls_a:
            aliases.add(cls_a)
        if p == path_a:
            aliases = aliases | ({cls_a} if cls_a else set())
        if not word.search(s_cur) and p != path_a:
            continue
        edits = []
        refs = _refs(s_cur, rt, aliases, name) if aliases else []
        bare = []
        if p == path_a:
            bare = [x for x in _walk(rt) if x.type == "identifier" and _t(s_cur, x) == name
                    and not (x.prev_sibling is not None and x.prev_sibling.type == ".")
                    and not (decl.start_byte <= x.start_byte < decl.end_byte)]
        inline = [m for m in re.finditer(rb"(?:preload|load)\s*\(\s*[\"']" + re.escape(res_a.encode()) + rb"[\"']\s*\)\s*\." + re.escape(name.encode()) + rb"\b", s_cur)
                  if not _in_str_or_comment(rt, m.start())]
        if not refs and not bare and not inline:
            continue
        if p == path_b:
            for x in refs:
                edits.append((x.start_byte, x.end_byte, b"@@SELF@@"))
        else:
            ref_name, add_line = ref_for_b_in(s_cur, rt, p)
            for x in refs + bare:
                edits.append((x.start_byte, x.end_byte, ref_name.encode() if x in refs else f"{ref_name}.{name}".encode()))
            for m in inline:
                edits.append((m.start(), m.end(), m.group(0).replace(res_a.encode(), res_b.encode())))
        new = _apply(s_cur, edits)
        if p == path_b:
            new = new.replace(b"@@SELF@@.", b"")
        elif add_line and (refs or bare):
            e2 = b"\r\n" if b"\r\n" in new else b"\n"
            rt2 = _parse(new)
            anchor = None
            for c in rt2.children:
                if c.type in ("const_statement", "extends_statement", "class_name_statement"):
                    anchor = c
            ins = e2 + add_line.encode()
            new = new[:anchor.end_byte] + ins + new[anchor.end_byte:] if anchor is not None else add_line.encode() + e2 + new
            if p == path_a:
                a_preloads_b = True
        if p != path_b:
            updated.append(p)
        out_files[p] = new
    # ---- cut from the old script
    cur_a = out_files.get(path_a, src_a)
    rt_a = _parse(cur_a)
    d2 = next(m[1] for m in _members(cur_a, rt_a) if m[0] == name)
    cs, pv = d2.start_byte, d2.prev_sibling
    while pv is not None and pv.type == "comment" and cur_a[pv.end_byte:cs].count(b"\n") <= 1:
        cs, pv = pv.start_byte, pv.prev_sibling
    cs = _line_start(cur_a, cs)
    ce = d2.end_byte
    while cur_a[ce:ce + 1] in (b"\r", b"\n"):
        ce += 1
    out_files[path_a] = cur_a[:cs] + cur_a[ce:]
    if updated and path_a not in updated:
        updated.append(path_a)

    # ---- preload cycle: a preloads b (added above) and b preloads a
    b_text = (src_b0 or b"") + b"\n".join(l.encode() for l in add_b_lines)
    b_preloads_a = res_a.encode() in b_text
    if (a_preloads_b or res_b.encode() in out_files[path_a]) and b_preloads_a:
        return (f"move_symbol: {path_a.name} and {path_b.name} would preload each other (a cycle Godot rejects); give one of "
                f"them a class_name. Nothing was written.")

    # ---- the target
    if src_b0 is None:
        head = (eol.join(l.encode() for l in add_b_lines) + eol + eol) if add_b_lines else b""
        text_b = b"extends RefCounted" + eol + eol + head + moved.rstrip(b"\r\n") + eol
    else:
        base = out_files.get(path_b, src_b0)
        rtb = _parse(base)
        text_b = base
        add_l = [l for l in add_b_lines if l.encode() not in base]
        if add_l:
            anchor = None
            for c in rtb.children:
                if c.type in ("const_statement", "extends_statement", "class_name_statement"):
                    anchor = c
            ins = eol + eol.join(l.encode() for l in add_l)
            text_b = text_b[:anchor.end_byte] + ins + text_b[anchor.end_byte:] if anchor is not None else ins.lstrip() + eol + text_b
        text_b = text_b.rstrip(b"\r\n") + eol + eol + moved.rstrip(b"\r\n") + eol
    out_files[path_b] = text_b

    if apply:
        for p, data in out_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    verb = "moved" if apply else "would move"
    lines = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
             f"{verb} {kind} {name}: {rel_a} -> {rel(path_b)}"]
    if updated:
        lines.append(f"files updated: {len(updated)}")
        lines += [f"  {rel(q)}" for q in updated[:12]]
    lines.append("Strings, call(\"name\") and Callable lookups were not rewritten.")
    if apply:
        lines += ["", "Done: the member and the references to it are already changed. Do NOT Read or Edit these files again to "
                      "redo it.", "Next: run the project / the tests."]
    return "\n".join(lines)
