"""move_symbol for Rust: move a top-level fn, struct, enum, union, trait, type alias, const or static to another module
file of the same crate.

A Rust file is a module; `use` declarations and paths name items by module path. So moving an item means:

* the item (with its docs, attributes and, for a struct/enum/union, the inherent and trait `impl` blocks of the same file)
  goes to the new file; `mod new;` is declared in the parent module when the file is new;
* the moved code gets the old file's `use` lines it needs (paths made absolute with `crate::`) and `use` lines for the
  old module's items it names; `self::` / `super::` paths inside it are made absolute;
* every `use` that named the item through the old module, and every path that named it, is rewritten to the new
  module; modules that glob-imported the old module (`use old::*`) gain a `use new::Name`, `pub use` globs gain a
  `pub use` so facade modules keep exporting it; the old file imports it back when it still uses it;
* a `pub` item of a publicly reachable module keeps a `pub use` re-export in the old file (the crate API doesn't move);
* private fields and methods that the old module still reaches, and a private item itself, are widened to
  `pub(crate)`; private items of the old module the moved code needs, macros, `#[path]` modules and bin/test crates
  are refusals.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP = {"target", ".git", "node_modules", "vendor"}
_parser = None
_KINDS = {"function_item": "fn", "struct_item": "struct", "enum_item": "enum", "union_item": "union",
          "trait_item": "trait", "type_item": "type", "const_item": "const", "static_item": "static"}
_TRAITISH = {"Write", "Read", "BufRead", "Seek", "FromStr", "Hash", "Hasher", "Borrow", "BorrowMut", "Any", "Buf",
             "BufMut", "Itertools", "FromIterator", "Extend", "Sum", "Product", "Deref", "DerefMut"}
WS = (b" ", bytes([9]), bytes([13]), bytes([10]))
EOLS = (bytes([13]), bytes([10]))
_DOC = ("attribute_item", "line_comment", "block_comment")


def _parse(src: bytes):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_rust as tr
        _parser = ts.Parser(ts.Language(tr.language()))
    return _parser.parse(src).root_node


def _t(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _rs_files(root: Path) -> list:
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in _SKIP and not d.startswith("."))
        out += [Path(dp) / f for f in sorted(fn) if f.endswith(".rs")]
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


def _crate(path: Path):
    for d in [path.parent, *path.parents]:
        c = d / "Cargo.toml"
        if c.exists():
            txt = c.read_text(encoding="utf-8", errors="replace")
            if re.search(r"(?m)^\[package\]", txt):
                m = re.search(r'(?ms)^\[package\].*?^name\s*=\s*"([^"]+)"', txt)
                return d, (m.group(1) if m else d.name).replace("-", "_")
    return None, None


def _vis(src: bytes, n) -> str:
    for c in n.children:
        if c.type == "visibility_modifier":
            return re.sub(r"\s+", "", _t(src, c))
    return ""


def _name(src: bytes, n):
    x = n.child_by_field_name("name")
    return _t(src, x) if x is not None else None


def _doc_start(src: bytes, n) -> int:
    start, prev = n.start_byte, n.prev_sibling
    while prev is not None and prev.type in _DOC and src[prev.end_byte:start].count(b"\n") <= 1:
        start, prev = prev.start_byte, prev.prev_sibling
    return start


def _module_tree(crate_dir: Path):
    """modpath -> file, file -> modpath, modpath -> (visibility of its `mod` line); None when #[path] is used."""
    src_dir = crate_dir / "src"
    root = next((src_dir / n for n in ("lib.rs", "main.rs") if (src_dir / n).exists()), None)
    if root is None:
        return None
    mods, files, vis = {(): root}, {root: ()}, {}
    queue = [(root, (), src_dir)]
    while queue:
        path, mp, base = queue.pop()
        data = path.read_bytes()

        def scan(container, mpath, dirbase):
            for ch in container.children:
                if ch.type != "mod_item":
                    continue
                nm = _name(data, ch)
                prev = ch.prev_sibling
                while prev is not None and prev.type == "attribute_item":
                    if re.search(r"\bpath\s*=", _t(data, prev)):
                        return False
                    prev = prev.prev_sibling
                body = ch.child_by_field_name("body")
                sub = mpath + (nm,)
                vis[sub] = _vis(data, ch)
                if body is not None:
                    mods.setdefault(sub, path)
                    if scan(body, sub, dirbase / nm) is False:
                        return False
                    continue
                for cand, child_base in ((dirbase / f"{nm}.rs", dirbase / nm), (dirbase / nm / "mod.rs", dirbase / nm)):
                    if cand.exists():
                        mods[sub], files[cand] = cand, sub
                        queue.append((cand, sub, child_base))
                        break
            return True
        if scan(_parse(data), mp, base) is False:
            return None
    return mods, files, vis


def _path_segs(src: bytes, n) -> list:
    if n is None:
        return []
    if n.type in ("scoped_identifier", "scoped_type_identifier"):
        return _path_segs(src, n.child_by_field_name("path")) + [_t(src, n.child_by_field_name("name"))]
    if n.type == "generic_type":
        return _path_segs(src, n.child_by_field_name("type"))
    return [_t(src, n)]


def _flat(src: bytes, n, prefix: list, out: list, plist):
    t = n.type
    if t in ("identifier", "self", "crate", "super", "scoped_identifier"):
        segs = prefix + _path_segs(src, n)
        out.append(dict(kind="name", segs=segs, bound=(segs[-2] if segs[-1] == "self" and len(segs) > 1 else segs[-1]), node=n, plist=plist))
    elif t == "use_as_clause":
        segs = prefix + _path_segs(src, n.child_by_field_name("path"))
        out.append(dict(kind="name", segs=segs, bound=_t(src, n.child_by_field_name("alias")), node=n, plist=plist, alias=True))
    elif t == "use_wildcard":
        p = [c for c in n.children if c.type not in ("*", "::")]
        out.append(dict(kind="glob", segs=prefix + (_path_segs(src, p[0]) if p else []), bound=None, node=n, plist=plist))
    elif t == "scoped_use_list":
        np = prefix + _path_segs(src, n.child_by_field_name("path"))
        lst = n.child_by_field_name("list")
        for ch in lst.children:
            if ch.is_named:
                _flat(src, ch, np, out, lst)
    elif t == "use_list":
        for ch in n.children:
            if ch.is_named:
                _flat(src, ch, prefix, out, n)


def _use_leaves(src: bytes, ud) -> list:
    out = []
    for ch in ud.children:
        if ch.is_named and ch.type != "visibility_modifier":
            _flat(src, ch, [], out, None)
    return out


def _ctx_of(n, fmod: tuple, src: bytes) -> tuple:
    names, p = [], n.parent
    while p is not None:
        if p.type == "mod_item" and p.child_by_field_name("body") is not None:
            names.append(_name(src, p))
        p = p.parent
    return fmod + tuple(reversed(names))


def _resolve(segs: list, ctx: tuple, alias: dict, mods: dict, local=(), globs=()):
    """Absolute module path of a path prefix, or None when it can't be told."""
    if not segs:
        return ctx
    first = segs[0]
    if first in local:
        return ctx + tuple(segs)
    if first == "crate":
        return tuple(segs[1:])
    if first == "self":
        return ctx + tuple(segs[1:])
    if first == "super":
        base, rest = ctx, list(segs)
        while rest and rest[0] == "super":
            if not base:
                return None
            base, rest = base[:-1], rest[1:]
        return base + tuple(rest)
    if first in alias:
        return alias[first] + tuple(segs[1:])
    if ctx + (first,) in mods:
        return ctx + tuple(segs)
    for g in globs:
        if g + (first,) in mods:
            return g + tuple(segs)
    return None


def _visible(vis: str, defmod: tuple, frommod: tuple) -> bool:
    if vis == "pub" or vis == "pub(crate)":
        return True
    if vis == "pub(super)":
        return frommod[:max(len(defmod) - 1, 0)] == defmod[:-1]
    if vis.startswith("pub(in"):
        m = re.match(r"pub\(incrate((?:::\w+)*)\)", vis)
        if m:
            tgt = tuple(x for x in m.group(1).split("::") if x)
            return frommod[:len(tgt)] == tgt
        return True
    return frommod[:len(defmod)] == defmod


def _top_items(src: bytes, root) -> list:
    """(name, node, kind) of the named top-level items of a file."""
    out = []
    for c in root.children:
        if c.type in _KINDS:
            out.append((_name(src, c), c, _KINDS[c.type]))
        elif c.type == "mod_item":
            out.append((_name(src, c), c, "mod"))
        elif c.type in ("macro_definition",):
            out.append((_name(src, c), c, "macro"))
    return out


def _impl_self(src: bytes, n) -> str:
    t = n.child_by_field_name("type")
    while t is not None and t.type in ("generic_type", "reference_type"):
        t = t.child_by_field_name("type") or (t.children[-1] if t.children else None)
    return _t(src, t) if t is not None else ""


def _idents(src: bytes, n) -> set:
    return {_t(src, x) for x in _walk(n) if x.type in ("identifier", "type_identifier")}


def _mentions(src: bytes, root, name: str) -> bool:
    for x in _walk(root):
        if x.type in ("identifier", "type_identifier") and _t(src, x) == name:
            p = x.parent
            if p is not None and p.type in ("scoped_identifier", "scoped_type_identifier") and p.child_by_field_name("name") == x:
                continue
            q = p
            while q is not None and q.type != "use_declaration":
                q = q.parent
            if q is None:
                return True
    return False


def _use_insert(src: bytes, root, eol: bytes) -> tuple:
    """(offset, text before, text after) for new top-level `use` lines."""
    last = None
    for c in root.children:
        if c.type == "use_declaration":
            last = c
    if last is not None:
        return last.end_byte, eol, b""
    pos = 0
    for c in root.children:
        if c.type == "inner_attribute_item" or (c.type in ("line_comment", "block_comment") and _t(src, c).startswith(("//!", "/*!"))):
            pos = c.end_byte
        else:
            break
    if pos:
        return pos, eol + eol, b""
    return pos, b"", eol


def _drop_leaf(lf) -> list:
    """Edits that delete one leaf from the `{...}` list of a use declaration."""
    node, lst = lf["node"], lf["plist"]
    kids = lst.children
    i = kids.index(node)
    nxt = kids[i + 1] if i + 1 < len(kids) else None
    prv = kids[i - 1] if i > 0 else None
    if nxt is not None and nxt.type == ",":
        j = i + 2
        stop = kids[j].start_byte if j < len(kids) else nxt.end_byte
        return [(node.start_byte, stop, b"")]
    if prv is not None and prv.type == ",":
        return [(prv.start_byte, node.end_byte, b"")]
    return [(node.start_byte, node.end_byte, b"")]


def _tidy(src: bytes) -> bytes:
    return re.sub(rb"\n{3,}", b"\n\n", src.replace(b"\r\n", b"\n")) if b"\r\n" not in src else src


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: Rust items move one at a time; pass one symbol."
    node, _o = resolve(cx, symbol.strip())
    if node is None:                       # a const or static may not be a graph node: path::name finds it in the file
        where, _s, nm0 = symbol.strip().rpartition("::")
        if where.endswith(".rs"):
            node = {"file_path": where, "label": nm0}
        else:
            rows = [r for r in cx.execute("SELECT * FROM nodes WHERE type='variable' AND label=?", (nm0,)).fetchall()
                    if (r["file_path"] or "").lower().endswith(".rs")]
            if len({r["file_path"] for r in rows}) == 1:
                node = rows[0]
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
    if path_b.suffix != ".rs":
        return "move_symbol: to_file must be a .rs file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."
    name = node["label"].split(".")[-1]
    if "." in node["label"]:
        return f"move_symbol: {node['label']} is a method; it moves with its type. Pass the type instead."

    crate_dir, crate_name = _crate(path_a)
    if crate_dir is None:
        return "move_symbol: no Cargo.toml with a [package] above this file; nothing was written."
    tree = _module_tree(crate_dir)
    if tree is None:
        return "move_symbol: the crate has no src/lib.rs or src/main.rs, or uses #[path] modules; nothing was written."
    mods, files, mvis = tree
    if path_a not in files:
        return f"move_symbol: {rel_a} is not reached by any `mod` declaration of its crate; nothing was written."
    old_mod = files[path_a]
    parent_edit = None
    if path_b in files:
        new_mod = files[path_b]
    else:
        try:
            parts = list(path_b.relative_to(crate_dir / "src").parts)
        except ValueError:
            return "move_symbol: to_file must be under the crate's src/ directory; nothing was written."
        if parts[-1] == "mod.rs":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][:-3]
        if not parts or parts[0] == "bin" or parts[-1] in ("lib", "main") and len(parts) == 1:
            return "move_symbol: to_file can't be a crate root or a src/bin file; nothing was written."
        new_mod = tuple(parts)
        pm = new_mod[:-1]
        if pm not in mods:
            return f"move_symbol: the parent module crate::{'::'.join(pm)} does not exist; nothing was written."
        pfile = mods[pm]
        base = pfile.parent if pfile.name in ("mod.rs", "lib.rs", "main.rs") else pfile.parent / pfile.stem
        if path_b.parent != base and path_b.parent.parent != base:
            return "move_symbol: to_file is not where the parent module looks for its `mod` file; nothing was written."
        parent_edit = (pfile, new_mod[-1])
    if new_mod == old_mod:
        return "move_symbol: the symbol is already in that module."

    src_a = path_a.read_bytes()
    root_a = _parse(src_a)
    eol_a = b"\r\n" if b"\r\n" in src_a else b"\n"
    tops = _top_items(src_a, root_a)
    hits = [t for t in tops if t[0] == name]
    if not hits:
        return f"move_symbol: {name} is not a top-level fn, struct, enum, union, trait, type, const or static of {rel_a}; nothing was written."
    if len(hits) > 1 or hits[0][2] in ("mod", "macro"):
        return f"move_symbol: {name} is declared more than once or is a module/macro in {rel_a}; nothing was written."
    _n, item, kind = hits[0]
    if any(t[2] == "macro" for t in tops) and any(t[0] in _idents(src_a, item) for t in tops if t[2] == "macro"):
        return "move_symbol: the item uses a macro_rules! macro defined in its file; nothing was written."

    # the pieces that leave: the item and the impl blocks of its type in this file
    pieces = [item]
    if kind in ("struct", "enum", "union"):
        pieces += [c for c in root_a.children if c.type == "impl_item" and _impl_self(src_a, c) == name]
    ranges = []
    for p in pieces:
        end = p.end_byte
        while src_a[end:end + 1] in WS:
            end += 1
        ranges.append((_doc_start(src_a, p), end))
    old_items = {t[0]: t for t in tops if t[0] and t[0] != name}
    new_text_path = "::".join(("crate",) + new_mod)
    full = new_text_path + "::" + name

    # ---- what the moved code needs
    used = set()
    for p in pieces:
        used |= _idents(src_a, p)
    local = set(old_items) | {name}

    def alias_of(src, rt, fmod):
        al = {}
        for ud in (x for x in _walk(rt) if x.type == "use_declaration"):
            ctx = _ctx_of(ud, fmod, src)
            for lf in _use_leaves(src, ud):
                if lf["kind"] == "name" and lf["segs"][-1] != "self":
                    pm = _resolve(lf["segs"], ctx, {}, mods)
                    if pm is not None and pm in mods:
                        al[lf["bound"]] = pm
        return al

    al_a = alias_of(src_a, root_a, old_mod)

    def old_item_ok(tgt):
        if tgt is not None and tgt[:len(old_mod)] == old_mod and len(tgt) > len(old_mod):
            it = old_items.get(tgt[len(old_mod)])
            if it is not None and not _visible(_vis(src_a, it[1]), old_mod, new_mod):
                return it[0]
        return None

    _ct = []

    def crate_traits():
        if not _ct:
            found = set()
            for f2, mp in files.items():
                for m in re.finditer(rb"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?trait\s+(\w+)", f2.read_bytes()):
                    found.add(mp + (m.group(1).decode(),))
            _ct.append(found)
        return _ct[0]

    carry = []                                  # (bound name or None, use line)
    for ud in (c for c in root_a.children if c.type == "use_declaration"):
        for lf in _use_leaves(src_a, ud):
            segs = lf["segs"]
            if lf["kind"] == "glob":
                pm = _resolve(segs, old_mod, al_a, mods, local)
                bad = old_item_ok(pm)
                if bad:
                    return f"move_symbol: the glob import `{'::'.join(segs)}::*` reaches {bad}, private to {rel_a}; nothing was written."
                if pm == new_mod:
                    continue
                carry.append((None, "use " + ("::".join(("crate",) + pm) if pm is not None else "::".join(segs)) + "::*;", None))
                continue
            if segs[-1] == "self":
                if len(segs) < 2:
                    continue
                segs = segs[:-1]
            bound = lf["bound"]
            pre = _resolve(segs[:-1], old_mod, al_a, mods, local)
            tgt = (pre + (segs[-1],)) if pre is not None else None
            trait_ish = bound[:1].isupper() and (bound in _TRAITISH or bound.endswith("Ext") or (tgt is not None and tgt in crate_traits()))
            if bound not in used and not trait_ish:
                continue
            bad = old_item_ok(tgt)
            if bad:
                return f"move_symbol: the item uses {bound}, imported from {bad} which is private to {rel_a}; nothing was written."
            path_txt = "::".join(("crate",) + tgt) if tgt is not None else "::".join(segs)
            if tgt is not None and tgt[:-1] == new_mod and not lf.get("alias"):
                continue
            carry.append((bound, f"use {path_txt}" + (f" as {bound};" if lf.get("alias") else ";"), tgt))
    carried = {c[0] for c in carry if c[0]}
    for nm in sorted(used):
        if nm in old_items and nm not in carried:
            it = old_items[nm]
            if it[2] == "macro":
                return f"move_symbol: the item uses the macro {nm}! defined in {rel_a}; nothing was written."
            if not _visible(_vis(src_a, it[1]), old_mod, new_mod):
                return (f"move_symbol: {name} uses {nm}, a private item of {rel_a} that the new module can't reach; "
                        f"make it pub(crate) or move it too. Nothing was written.")
            carry.append((nm, "use " + "::".join(("crate",) + old_mod + (nm,)) + ";", old_mod + (nm,)))

    src_b0 = path_b.read_bytes() if path_b.exists() else None
    if src_b0 is not None and any(t[0] == name for t in _top_items(src_b0, _parse(src_b0))):
        return f"move_symbol: {path_b.name} already declares {name}; nothing was written."

    # ---- the moved text: self::/super:: made absolute, visibilities settled
    scope_files = [f for f, m in files.items() if f not in (path_a, path_b) and m[:len(old_mod)] == old_mod]
    remaining = _apply(src_a, [(s, e, b"") for s, e in ranges])
    scope_text = remaining.decode("utf-8", errors="replace") + "\n" + "\n".join(
        f.read_bytes().decode("utf-8", errors="replace") for f in scope_files)
    scope_words = set(re.findall(r"[A-Za-z_]\w*", scope_text))
    widened = []

    def piece_edits(p):
        ed, starts = [], set()
        for x in _walk(p):
            if x.type in ("scoped_identifier", "scoped_type_identifier", "scoped_use_list", "use_wildcard") \
                    and x.start_byte not in starts and re.match(rb"(self|super)\b", src_a[x.start_byte:x.end_byte]):
                starts.add(x.start_byte)
                m = re.match(rb"(?:self|super)(?:::super)*", src_a[x.start_byte:x.end_byte])
                pm = _resolve(m.group(0).decode().split("::"), old_mod, {}, mods)
                if pm is None:
                    return None
                ed.append((x.start_byte, x.start_byte + m.end(), "::".join(("crate",) + pm).encode()))
            elif x.type == "visibility_modifier":
                v = re.sub(r"\s+", "", _t(src_a, x))
                if v in ("pub(super)", "pub(self)") or v.startswith("pub(in"):
                    ed.append((x.start_byte, x.end_byte, b"pub(crate)"))
        return ed

    def widen_node(n, nm, ed):
        if _vis(src_a, n) in ("pub", "pub(crate)") or nm not in scope_words:
            return
        vm = next((c for c in n.children if c.type == "visibility_modifier"), None)
        ed.append((vm.start_byte, vm.end_byte, b"pub(crate)") if vm is not None else (n.start_byte, n.start_byte, b"pub(crate) "))
        widened.append(nm)

    def widen_members(p, ed):
        inherent = p.type == "impl_item" and p.child_by_field_name("trait") is None
        for x in _walk(p):
            if x.type == "field_declaration" and p.type in ("struct_item", "union_item"):
                widen_node(x, _name(src_a, x), ed)
            elif x.type == "ordered_field_declaration_list" and p.type == "struct_item" and x.parent == p and name in scope_words:
                for ch in x.children:
                    if ch.is_named and ch.type not in ("visibility_modifier", "attribute_item"):
                        vm = ch.prev_sibling
                        if not (vm is not None and vm.type == "visibility_modifier" and _t(src_a, vm).startswith("pub")):
                            ed.append((ch.start_byte, ch.start_byte, b"pub(crate) "))
                            widened.append(name)
            elif inherent and x.type in ("function_item", "const_item", "type_item") and x.parent is not None and x.parent.parent == p:
                widen_node(x, _name(src_a, x), ed)

    for p in pieces:                         # a path into a nested inline module whose item the new module can't reach
        for x in _walk(p):
            if x.type in ("scoped_identifier", "scoped_type_identifier"):
                path = _path_segs(src_a, x)
                if len(path) >= 2 and path[0] in old_items and old_items[path[0]][2] == "mod":
                    body = old_items[path[0]][1].child_by_field_name("body")
                    for ch in (body.children if body is not None else []):
                        if ch.type in _KINDS and _name(src_a, ch) == path[1]                                 and not _visible(_vis(src_a, ch), old_mod + (path[0],), new_mod):
                            return (f"move_symbol: {name} uses {path[0]}::{path[1]}, whose visibility the new module can't "
                                    f"reach; nothing was written.")
    used_fields = set()
    for p in pieces:
        used_fields |= {_t(src_a, x) for x in _walk(p) if x.type in ("field_identifier", "shorthand_field_identifier")}
        used_fields |= {_t(src_a, x.children[0]) for x in _walk(p) if x.type == "shorthand_field_initializer" and x.children}
    old_widen, widened_old = [], []
    for c in root_a.children:
        if c in pieces or c.type not in ("struct_item", "union_item", "impl_item"):
            continue
        if c.type == "impl_item" and c.child_by_field_name("trait") is not None:
            continue
        if c.type == "impl_item" and _impl_self(src_a, c) == name:
            continue
        for x in _walk(c):
            if x.type == "field_declaration" and c.type != "impl_item":
                nm_f = _name(src_a, x)
                tgt_n = x
            elif x.type == "function_item" and c.type == "impl_item" and x.parent is not None and x.parent.parent is c:
                nm_f, tgt_n = _name(src_a, x), x
            else:
                continue
            if nm_f in used_fields and _vis(src_a, tgt_n) not in ("pub", "pub(crate)"):
                vm = next((q for q in tgt_n.children if q.type == "visibility_modifier"), None)
                old_widen.append((vm.start_byte, vm.end_byte, b"pub(crate)") if vm is not None
                                 else (tgt_n.start_byte, tgt_n.start_byte, b"pub(crate) "))
                widened_old.append(nm_f)
    if old_widen:
        remaining = _apply(src_a, [(a, b, b"") for a, b in ranges] + old_widen)
    chunks = []
    for p, (s, e) in zip(pieces, ranges):
        ed = piece_edits(p)
        if ed is None:
            return "move_symbol: a `super::` path in the item leaves the crate root; nothing was written."
        widen_members(p, ed)
        if p == item:
            widen_node(p, name, ed)
        sub = [(a - s, b - s, r) for a, b, r in ed if s <= a and b <= e]
        chunks.append(_apply(src_a[s:p.end_byte], sub))
    item_vis = _vis(src_a, item)

    old_pub = (crate_dir / "src" / "lib.rs").exists() and all(mvis.get(old_mod[:i + 1], "") == "pub" for i in range(len(old_mod)))
    keep_reexport = item_vis == "pub" and old_pub

    # ---- every other file: imports and paths that named the item through the old module
    word = re.compile(rb"\b" + re.escape(name.encode()) + rb"\b")
    out_files, updated = {}, []

    def rewrite_file(f, fmod, src):
        rt = _parse(src)
        eol_f = b"\r\n" if b"\r\n" in src else b"\n"
        al = alias_of(src, rt, fmod)
        defined = any(t[0] == name for t in _top_items(src, rt)) and f != path_b
        edits, explicit, globs = [], False, []
        gm = []
        for ud in (x for x in _walk(rt) if x.type == "use_declaration"):
            for lf in _use_leaves(src, ud):
                if lf["kind"] == "glob":
                    g = _resolve(lf["segs"], _ctx_of(ud, fmod, src), al, mods)
                    if g is not None:
                        gm.append(g)
        for ud in (x for x in _walk(rt) if x.type == "use_declaration"):
            ctx = _ctx_of(ud, fmod, src)
            leaves = _use_leaves(src, ud)
            vis = _vis(src, ud)
            for lf in leaves:
                if lf["kind"] == "glob" and _resolve(lf["segs"], ctx, al, mods) == old_mod:
                    globs.append((ud, vis, ctx))
            hit = next((lf for lf in leaves if lf["kind"] == "name" and lf["segs"][-1] == name
                        and _resolve(lf["segs"][:-1], ctx, al, mods, globs=gm) == old_mod), None)
            if hit is None:
                continue
            explicit = True
            alias_txt = f" as {hit['bound']}" if hit.get("alias") else ""
            newdecl = b"" if f == path_b and not hit.get("alias") else eol_f + ((vis + " ") if vis else "").encode() + f"use {full}{alias_txt};".encode()
            if len(leaves) == 1:
                end = ud.end_byte
                if not newdecl:
                    while src[end:end + 1] in (b"\r", b"\n"):
                        end += 1
                edits.append((ud.start_byte, end, newdecl.lstrip(b"\r\n")))
                continue
            edits += _drop_leaf(hit)
            if newdecl:
                edits.append((ud.end_byte, ud.end_byte, newdecl))
        for x in _walk(rt):
            if x.type in ("scoped_identifier", "scoped_type_identifier") and _t(src, x.child_by_field_name("name")) == name:
                q, inside = x.parent, False
                while q is not None:
                    if q.type == "use_declaration":
                        inside = True
                        break
                    q = q.parent
                if inside:
                    continue
                segs = _path_segs(src, x.child_by_field_name("path"))
                if segs and _resolve(segs, _ctx_of(x, fmod, src), al, mods, globs=gm) == old_mod:
                    edits.append((x.start_byte, x.end_byte, full.encode()))
        if f != path_a and f != path_b and not defined and not explicit:
            seen_ctx = set()
            for ud, vis, ctx in globs:
                pub = vis.startswith("pub")
                if ctx in seen_ctx or not (pub or kind == "trait" or _mentions(src, rt, name)):
                    continue
                seen_ctx.add(ctx)
                edits.append((ud.end_byte, ud.end_byte, eol_f + ((vis + " ") if pub else "").encode() + f"use {full};".encode()))
        return edits

    def mod_decl(text, rt, nm):
        decls = [c for c in rt.children if c.type == "mod_item" and _name(text, c) == nm and c.child_by_field_name("body") is None]
        if decls:
            return text
        eol_f = b"\r\n" if b"\r\n" in text else b"\n"
        mods_ = [c for c in rt.children if c.type == "mod_item"]
        line = f"pub(crate) mod {nm};".encode()
        if mods_:
            pos = mods_[-1].end_byte
            return text[:pos] + eol_f + line + text[pos:]
        pos, before, after = _use_insert(text, rt, eol_f)
        return text[:pos] + before + line + after + text[pos:] if pos else line + eol_f + text

    parent_file, parent_name = parent_edit if parent_edit else (None, None)
    for f, fm in files.items():
        if f == path_a:
            data = remaining
        else:
            data = f.read_bytes()
            if f != path_b and not (word.search(data) or (b"*" in data and b"use" in data)) and f != parent_file:
                continue
        edits = rewrite_file(f, fm, data)
        text = _apply(data, edits) if edits else data
        if f == path_a:
            rt2 = _parse(text)
            eol_f = eol_a
            add = []
            if keep_reexport:
                add.append(f"pub use {full};")
            elif kind == "trait" or _mentions(text, rt2, name):
                add.append(f"use {full};")
            drop = []
            for ud in (c for c in rt2.children if c.type == "use_declaration"):
                if _vis(text, ud):
                    continue
                lvs = _use_leaves(text, ud)
                gone = [lf for lf in lvs if lf["kind"] == "name" and lf["bound"] in used and lf["bound"] != "self"
                        and lf["segs"][-1] != "self" and not _mentions(text, rt2, lf["bound"])
                        and not (lf["bound"][:1].isupper() and (lf["bound"] in _TRAITISH or lf["bound"].endswith("Ext")))
                        and (lf["bound"] != name)]
                if gone and len(gone) == len(lvs):
                    end = ud.end_byte
                    while text[end:end + 1] in EOLS:
                        end += 1
                    drop.append((ud.start_byte, end, b""))
                else:
                    for lf in gone:
                        if lf["plist"] is not None:
                            drop += _drop_leaf(lf)
            if drop:
                text = _apply(text, drop)
                rt2 = _parse(text)
            if add:
                pos, before, after = _use_insert(text, rt2, eol_f)
                text = text[:pos] + before + eol_f.join(a.encode() for a in add) + (eol_f if after else b"") + text[pos:] \
                    if before else text[:pos] + eol_f.join(a.encode() for a in add) + eol_f + text[pos:]
        if f == parent_file:
            text = mod_decl(text, _parse(text), parent_name)
        if text != (src_a if f == path_a else data) or f == path_a:
            out_files[f] = text
            if f != path_a and f != path_b:
                updated.append(f)

    # ---- the target file
    moved_text = (eol_a + eol_a).join(c.rstrip() for c in chunks).replace(b"\r\n", b"\n").replace(b"\n", eol_a) + eol_a
    if src_b0 is not None:
        base_b = out_files.get(path_b, src_b0)
        eol_b = b"\r\n" if b"\r\n" in base_b else b"\n"
        rt_b = _parse(base_b)
        have = {}
        for ud in (c for c in rt_b.children if c.type == "use_declaration"):
            for lf in _use_leaves(base_b, ud):
                if lf["kind"] == "name" and lf["segs"][-1] != "self":
                    pre = _resolve(lf["segs"][:-1], new_mod, {}, mods)
                    have[lf["bound"]] = (pre + (lf["segs"][-1],)) if pre is not None else tuple(lf["segs"])
        have_lines = {_t(base_b, c) for c in rt_b.children if c.type == "use_declaration"}
        defined_b = {t[0] for t in _top_items(base_b, rt_b)}
        have_globs = set()
        for ud in (c for c in rt_b.children if c.type == "use_declaration"):
            for lf in _use_leaves(base_b, ud):
                if lf["kind"] == "glob":
                    g = _resolve(lf["segs"], new_mod, {}, mods)
                    if g is not None:
                        have_globs.add(g)
        lines = []
        for b, ln, tg in carry:
            if b and tg is not None and tg[:-1] in have_globs and " as " not in ln and b not in have:
                continue                        # a glob import of the same module already brings it (and may re-export it)
            if ln in have_lines or (b and (b in have or b in defined_b)):
                if b and b in have and tg is not None and have[b] != tg:
                    return f"move_symbol: {path_b.name} already imports a different {b}; nothing was written."
                continue
            lines.append(ln)
        text_b = base_b
        if lines:
            pos, before, after = _use_insert(text_b, rt_b, eol_b)
            ins = eol_b.join(l.encode() for l in lines)
            text_b = text_b[:pos] + (before + ins if before else ins + eol_b) + text_b[pos:] if pos else ins + eol_b + eol_b + text_b
        text_b = (text_b if text_b.endswith(b"\n") else text_b + eol_b) + eol_b + moved_text.replace(eol_a, eol_b)
    else:
        seen_l, lines = set(), []
        for _b, ln, _tg in carry:
            if ln not in seen_l:
                seen_l.add(ln)
                lines.append(ln)
        head = (eol_a.join(l.encode() for l in lines) + eol_a + eol_a) if lines else b""
        text_b = head + moved_text
    out_files[path_b] = text_b

    if apply:
        for p, data in out_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    rel = lambda p: p.relative_to(root).as_posix() if str(p).startswith(str(root)) else str(p)
    old_t, new_t = "::".join(("crate",) + old_mod), "::".join(("crate",) + new_mod)
    verb = "moved" if apply else "would move"
    outl = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
            f"{verb} {kind} {name} from {old_t} to {new_t}: {rel_a} -> {rel(path_b)}"]
    if parent_edit:
        outl.append(f"declared `pub(crate) mod {parent_name};` in {rel(parent_file)}")
    if keep_reexport:
        outl.append(f"{rel_a} keeps `pub use {full};` so the crate's public path still works.")
    if widened_old:
        outl.append(f"widened to pub(crate) in {rel_a} because the moved code reaches them: {', '.join(sorted(set(widened_old)))}")
    if widened:
        outl.append(f"widened to pub(crate) because the old module still reaches them: {', '.join(sorted(set(widened)))}")
    if updated:
        outl.append(f"files updated: {len(updated)}")
        outl += [f"  {rel(p)}" for p in updated[:12]]
        if len(updated) > 12:
            outl.append(f"  ... {len(updated) - 12} more")
    outl.append("Macro bodies, other crates (bin, tests, examples) and non-Rust files were not rewritten; unused `use` lines "
                "left behind are warnings (cargo fix removes them).")
    if apply:
        outl += ["", "Done: the item, its imports and every path to it are already changed. Do NOT Read or Edit these files again "
                     "to redo it.", "Next: cargo check, then check_change and the tests."]
    return "\n".join(outl)
