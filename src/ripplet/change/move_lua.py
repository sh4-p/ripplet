"""move_symbol for Lua: move a module function (`function M.f`, `function M:f`, `M.f = ...`), a `local function` or a global
function to another module file.

Lua code reaches a module function as `local m = require("mod"); m.f()`. So moving `M.f` from module `a` to module `b` means:

* the function goes into `b` (before its final `return`), its `M.` header now names b's module table; a new `b.lua` is
  `local M = {} ... return M`;
* in the moved code, a use of the old module table (`M.other`) becomes `a.other` with `local a = require("a")` added to
  `b`; `require` aliases it used are copied over; any other local of the old file it uses is a refusal, and so is a
  circular `require` (moved code needs `a` while `a` needs the moved function);
* every `m.f` where `m` is bound to `require("a")` (and `require("a").f`) is rewritten to the new module, with a
  `local b = require("b")` after the old one; the old file's own `M.f` uses too;
* a lazy forwarding stub (`function M.f(...) return require("b").f(...) end`) stays in `a` for methods (`M:f`, found through
  metatables) and when a mention of `f` could not be rewritten, so the old module's API keeps working.
Module names come from the `require` strings used in the project (or the path under lua/ or src/). A `local function` the
old file still uses, a global function (it stays global; the old file `require`s the new module where it stood) and constants
are handled; dynamic access (`a["f"]`, `_G`) is not rewritten.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP = {"node_modules", ".git", ".luarocks", "lua_modules", "build"}
_parser = None
_REQ = re.compile(r"(?:^|[^\w.])require\s*\(?\s*([\"'])([^\"']+)\1\s*\)?")
_ALIAS = re.compile(r"^local\s+(\w+)\s*=\s*require\s*\(?\s*([\"'])([^\"']+)\2\s*\)?\s*$")


def _parse(src: bytes):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_lua as tl
        _parser = ts.Parser(ts.Language(tl.language()))
    return _parser.parse(src).root_node


def _t(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="surrogateescape")


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _lua_files(root: Path) -> list:
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in _SKIP and not d.startswith("."))
        out += [Path(dp) / f for f in sorted(fn) if f.endswith(".lua")]
    return out


def _line_start(src: bytes, pos: int) -> int:
    return src.rfind(b"\n", 0, pos) + 1


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


def _entities(src: bytes, root) -> list:
    """(name, node, kind, table) for the top-level functions: kind field | method | local | global"""
    out = []
    for c in root.children:
        if c.type == "function_declaration":
            nm = next((x for x in c.children if x.type in ("identifier", "dot_index_expression", "method_index_expression")), None)
            if nm is None:
                continue
            if nm.type == "identifier":
                out.append((_t(src, nm), c, "local" if any(x.type == "local" for x in c.children) else "global", None))
            else:
                kids = [x for x in nm.children if x.type == "identifier"]
                out.append((_t(src, kids[-1]), c, "method" if nm.type == "method_index_expression" else "field", _t(src, kids[0])))
        elif c.type == "assignment_statement":
            vl = next((x for x in c.children if x.type == "variable_list"), None)
            if vl is not None and len(vl.named_children) == 1 and vl.named_children[0].type == "dot_index_expression":
                kids = [x for x in vl.named_children[0].children if x.type == "identifier"]
                if len(kids) == 2:
                    out.append((_t(src, kids[1]), c, "field", _t(src, kids[0])))
    return out


def _top_locals(src: bytes, root) -> tuple:
    """names declared by top-level `local`, and {alias: module} for the `local x = require("m")` lines"""
    names, aliases = set(), {}
    for c in root.children:
        if c.type == "variable_declaration":
            text = _t(src, c).strip()
            m = _ALIAS.match(text)
            if m:
                aliases[m.group(1)] = m.group(3)
            asg = next((x for x in c.children if x.type == "assignment_statement"), None)
            vl = next((x for x in (asg.children if asg is not None else c.children) if x.type in ("variable_list",)), None)
            for x in (vl.named_children if vl is not None else []):
                if x.type == "identifier":
                    names.add(_t(src, x))
        elif c.type == "function_declaration" and any(x.type == "local" for x in c.children):
            nm = next((x for x in c.children if x.type == "identifier"), None)
            if nm is not None:
                names.add(_t(src, nm))
    return names, aliases


def _returned(src: bytes, root):
    ret = [c for c in root.children if c.type == "return_statement"]
    if not ret:
        return None, None
    el = next((x for x in ret[-1].children if x.type == "expression_list"), None)
    if el is not None and len(el.named_children) == 1 and el.named_children[0].type == "identifier":
        return _t(src, el.named_children[0]), ret[-1]
    return None, ret[-1]


def _modname(path: Path, root: Path, files: dict) -> str:
    """module name of a file: the require string that reaches it, else its path under lua/ or src/"""
    rel = path.relative_to(root).with_suffix("").as_posix()
    cands = []
    for f, (s, _r) in files.items():
        for m in _REQ.finditer(s.decode("utf-8", "surrogateescape")):
            mod = m.group(2)
            tail = mod.replace(".", "/")
            if rel == tail or rel.endswith("/" + tail) or rel == tail + "/init" or rel.endswith("/" + tail + "/init"):
                cands.append(mod)
    if cands:
        return max(set(cands), key=lambda m: (len(m), cands.count(m)))
    for pre in ("lua/", "src/", "lib/"):
        if rel.startswith(pre):
            rel = rel[len(pre):]
            break
    if rel.endswith("/init"):
        rel = rel[:-5]
    return rel.replace("/", ".")


def _prefix_of(path: Path, root: Path, mod: str) -> str:
    rel = path.relative_to(root).with_suffix("").as_posix()
    tail = mod.replace(".", "/")
    if rel.endswith(tail):
        return rel[:len(rel) - len(tail)]
    if rel.endswith(tail + "/init"):
        return rel[:len(rel) - len(tail) - 5]
    return ""


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: Lua functions move one at a time; pass one symbol."
    node, _o = resolve(cx, symbol.strip())
    if node is None:
        where, _s, nm0 = symbol.strip().rpartition("::")
        if where.endswith(".lua"):
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
    if path_b.suffix != ".lua":
        return "move_symbol: to_file must be a .lua file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."

    src_a = path_a.read_bytes()
    root_a = _parse(src_a)
    name = re.split(r"[.:]", node["label"])[-1]
    ents = [e for e in _entities(src_a, root_a) if e[0] == name]
    if not ents:
        return f"move_symbol: {name} is not a top-level function of {rel_a}; nothing was written."
    if len(ents) > 1:
        return f"move_symbol: {name} is defined {len(ents)} times in {rel_a}; nothing was written."
    _n, decl, kind, ta = ents[0]
    locals_a, aliases_a = _top_locals(src_a, root_a)
    tab_a, ret_a = _returned(src_a, root_a)
    if kind in ("field", "method") and ta != tab_a:
        return f"move_symbol: {name} belongs to {ta}, which is not the table {rel_a} returns; nothing was written."

    files = {}
    for p in _lua_files(root):
        s = src_a if p == path_a else p.read_bytes()
        files[p] = (s, _parse(s))
    mod_a = _modname(path_a, root, files)
    src_b0 = path_b.read_bytes() if path_b.exists() else None
    if src_b0 is not None and any(e[0] == name for e in _entities(src_b0, _parse(src_b0))):
        return f"move_symbol: {path_b.name} already defines {name}; nothing was written."
    mod_b = None
    cands_b = _modname(path_b, root, files) if src_b0 is not None else None
    pre = _prefix_of(path_a, root, mod_a)
    if cands_b is not None:
        mod_b = cands_b
    else:
        relb = path_b.relative_to(root).with_suffix("").as_posix()
        mod_b = (relb[len(pre):] if pre and relb.startswith(pre) else relb).replace("/", ".")
        if mod_b.endswith(".init"):
            mod_b = mod_b[:-5]
    eol = b"\r\n" if b"\r\n" in src_a else b"\n"
    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")

    # ---- what the moved code uses
    text_d = _t(src_a, decl)
    dlocals = {_t(src_a, x) for x in _walk(decl) if x.type == "identifier" and x.parent is not None
               and x.parent.type in ("parameters", "variable_list", "for_generic_clause", "for_numeric_clause")}
    head_ids = set()
    for hc in decl.children:                 # the name being defined (`M.f`) is not a use of the module table
        if hc.type in ("dot_index_expression", "method_index_expression", "variable_list"):
            head_ids |= {(x.start_byte, x.end_byte) for x in _walk(hc)}
    used = {_t(src_a, x) for x in _walk(decl) if x.type == "identifier" and (x.start_byte, x.end_byte) not in head_ids} - dlocals
    uses_old_table = bool(ta and ta in used and kind in ("field", "method")) or (tab_a in used if tab_a else False)
    carry_lines = []
    for a_name, mod in aliases_a.items():
        if a_name in used and a_name not in dlocals:
            carry_lines.append((a_name, mod))
    bad = sorted(n for n in used & locals_a if n not in aliases_a and n != name and n not in dlocals and n != tab_a)
    if bad:
        return (f"move_symbol: {name} uses {', '.join(bad[:4])}, local to {rel_a} (a Lua local can't be reached from another "
                f"file); nothing was written.")

    # ---- the new module's table and requires
    if src_b0 is not None:
        root_b = _parse(src_b0)
        tab_b, ret_b = _returned(src_b0, root_b)
        if kind in ("field", "method") and tab_b is None:
            return f"move_symbol: {path_b.name} does not end with `return <table>`; nothing was written."
        locals_b, aliases_b = _top_locals(src_b0, root_b)
    else:
        root_b, tab_b, ret_b, locals_b, aliases_b = None, "M", None, set(), {}
    by_mod_b = {m: a for a, m in aliases_b.items()}
    if kind in ("field", "method") and src_b0 is None:
        tab_b = "M"
    alias_a_in_b = by_mod_b.get(mod_a) or re.sub(r"\W", "_", mod_a.split(".")[-1])
    if uses_old_table:
        if alias_a_in_b in locals_b and by_mod_b.get(mod_a) is None:
            alias_a_in_b += "_mod"

    # circular require: the moved code needs the old module, and the old module will need the new one
    remaining_uses = False
    rem_text = (src_a[:decl.start_byte] + src_a[decl.end_byte:]).decode("utf-8", "surrogateescape")
    if kind in ("field", "method") and re.search(r"\b" + re.escape(tab_a or "?") + r"[.:]" + re.escape(name) + r"\b", rem_text):
        remaining_uses = True
    if kind == "local" and re.search(r"\b" + re.escape(name) + r"\b", rem_text):
        return f"move_symbol: {rel_a} still uses the local function {name}; nothing was written."
    if uses_old_table and (remaining_uses or mod_b in aliases_a.values()):
        return (f"move_symbol: {name} needs {rel_a} (its module table) and {rel_a} calls {name}: moving it would make the two "
                f"modules require each other. Nothing was written.")

    # ---- the moved text
    d_edits = []
    base = decl.start_byte
    for x in _walk(decl):
        if x.type == "identifier" and ta and _t(src_a, x) == ta and kind in ("field", "method"):
            p = x.parent
            is_header = p is not None and p.type in ("dot_index_expression", "method_index_expression") and p.parent == decl
            is_assign_head = p is not None and p.type == "dot_index_expression" and p.parent is not None and p.parent.type == "variable_list" \
                and decl.type == "assignment_statement" and p.parent.parent == decl
            if is_header or is_assign_head:
                if x.parent.children[0] == x:
                    d_edits.append((x.start_byte - base, x.end_byte - base, tab_b.encode()))
            elif _t(src_a, x) not in dlocals:
                d_edits.append((x.start_byte - base, x.end_byte - base, alias_a_in_b.encode()))
    moved = _apply(src_a[base:decl.end_byte], d_edits)

    # doc comments directly above
    start = decl.start_byte
    prev = decl.prev_sibling
    while prev is not None and prev.type == "comment" and src_a[prev.end_byte:start].count(b"\n") <= 1:
        start, prev = prev.start_byte, prev.prev_sibling
    doc = src_a[start:base]
    moved = doc + moved

    # ---- other files: callers of old.f
    out_files: dict = {}
    updated = []
    a_loads_b = False
    unresolved = False
    word = re.compile(r"\b" + re.escape(name) + r"\b")
    for p, (s, r) in files.items():
        txt = s.decode("utf-8", "surrogateescape")
        if p != path_a and (not word.search(txt) or mod_a not in txt):
            continue
        aliases_f = {}
        for m in re.finditer(r"(?m)^[ \t]*local\s+(\w+)\s*=\s*require\s*\(?\s*([\"'])" + re.escape(mod_a) + r"\2\s*\)?[^\n]*", txt):
            aliases_f[m.group(1)] = m
        names_a = set(aliases_f)
        if p == path_a and tab_a:
            names_a.add(tab_a)
        edits, hits = [], 0
        for x in _walk(r):
            if x.type in ("dot_index_expression", "method_index_expression"):
                kids = [k for k in x.children if k.type == "identifier"]
                if len(kids) == 2 and _t(s, kids[1]) == name and _t(s, kids[0]) in names_a and kind in ("field", "method"):
                    if p == path_a and x.parent is not None and (x.parent.type == "function_declaration" or (
                            x.parent.type == "variable_list")) and decl.start_byte <= x.start_byte <= decl.end_byte:
                        continue
                    edits.append((kids[0].start_byte, kids[0].end_byte, b"@@B@@"))
                    hits += 1
            elif x.type == "function_call" and kind in ("field", "method"):
                pass
        for m in re.finditer(r"require\s*\(?\s*([\"'])" + re.escape(mod_a) + r"\1\s*\)?\s*\.\s*" + re.escape(name) + r"\b", txt):
            edits.append((m.start(), m.end(), m.group(0).replace(mod_a, mod_b, 1)))
            hits += 1
        total = len(word.findall(txt)) - (len(word.findall(_t(src_a, decl))) if p == path_a else 0)
        if p != path_a and total > hits and any(re.search(r"\b" + re.escape(a_) + r"\b", txt) for a_ in names_a):
            unresolved = True
        if p == path_a and total > hits:
            unresolved = unresolved or kind in ("method",)
        if not edits:
            continue
        if p == path_b:                      # the target now holds the function itself: its table
            out_files[p] = _apply(s, [(a_, b_, tab_b.encode() if c_ == b"@@B@@" else c_) for a_, b_, c_ in edits])
            continue
        # which alias names the new module in this file
        own = {m.group(1) for m in re.finditer(r"(?m)^[ \t]*local\s+(\w+)\s*=\s*require\s*\(?\s*([\"'])" + re.escape(mod_b) + r"\2", txt)}
        if own:
            ab = sorted(own)[0]
            add_line = None
        else:
            ab = re.sub(r"\W", "_", mod_b.split(".")[-1])
            if re.search(r"\b" + re.escape(ab) + r"\b", txt):
                ab += "_mod"
            add_line = f'local {ab} = require("{mod_b}")'
        edits = [(a_, b_, ab.encode() if c_ == b"@@B@@" else c_) for a_, b_, c_ in edits]
        new = _apply(s, edits)
        if p == path_a:
            a_loads_b = True
        if add_line:
            e2 = b"\r\n" if b"\r\n" in new else b"\n"
            rt = _parse(new)
            last = None
            for c in rt.children:
                if c.type == "variable_declaration" and _ALIAS.match(_t(new, c).strip()):
                    last = c
            if last is not None:
                new = new[:last.end_byte] + e2 + add_line.encode() + new[last.end_byte:]
            else:
                new = add_line.encode() + e2 + new
        out_files[p] = new
        if p != path_a:
            updated.append(p)

    stub = kind == "method" or unresolved
    # ---- the old file: cut + (stub | require-in-place)
    cur_a = out_files.get(path_a, src_a)
    rt_a = _parse(cur_a)
    ents2 = [e for e in _entities(cur_a, rt_a) if e[0] == name]
    d2 = ents2[0][1]
    cut_s, cut_e = d2.start_byte, d2.end_byte
    pv = d2.prev_sibling
    while pv is not None and pv.type == "comment" and cur_a[pv.end_byte:cut_s].count(b"\n") <= 1:
        cut_s, pv = pv.start_byte, pv.prev_sibling
    cut_s = _line_start(cur_a, cut_s)
    while cur_a[cut_e:cut_e + 1] in (b" ", bytes([9], )):
        cut_e += 1
    if cur_a[cut_e:cut_e + 2] == b"\r\n":
        cut_e += 2
    elif cur_a[cut_e:cut_e + 1] == b"\n":
        cut_e += 1
    repl = b""
    if stub and kind in ("field", "method"):
        sep = ":" if kind == "method" else "."
        args = "self, ..." if kind == "method" else "..."
        head = f"function {tab_a}{sep}{name}(...)"
        call = f'return require("{mod_b}").{name}({"self, ..." if kind == "method" else "..."})'
        repl = f"{head}{eol.decode()}  {call}{eol.decode()}end{eol.decode()}".encode()
    elif kind == "global":
        repl = f'require("{mod_b}"){eol.decode()}'.encode()
    new_a = cur_a[:cut_s] + repl + cur_a[cut_e:]
    if uses_old_table is False and kind in ("field", "method") and remaining_uses and not stub:
        pass
    out_files[path_a] = new_a

    # ---- the new file
    reqs = []
    for a_name, mod in carry_lines:
        if mod in by_mod_b.keys() and by_mod_b[mod] == a_name:
            continue
        reqs.append(f'local {a_name} = require("{mod}")')
    if uses_old_table and mod_a not in by_mod_b:
        reqs.append(f'local {alias_a_in_b} = require("{mod_a}")')
    block = (moved.rstrip(b"\r\n") + eol)
    if src_b0 is None:
        head = (eol.join(r.encode() for r in reqs) + eol) if reqs else b""
        tail = f"{eol.decode()}return {tab_b}{eol.decode()}".encode() if kind in ("field", "method") else b""
        top = f"local {tab_b} = {{}}{eol.decode()}".encode() if kind in ("field", "method") else b""
        text_b = head + (eol if head else b"") + top + eol + block + tail
        # the head lines come first, then the table
        text_b = (head + top + eol + block + tail) if head else (top + eol + block + tail)
    else:
        text_b = out_files.get(path_b, src_b0)
        if reqs:
            rtb = _parse(text_b)
            lastreq = None
            for c in rtb.children:
                if c.type == "variable_declaration" and _ALIAS.match(_t(text_b, c).strip()):
                    lastreq = c
            ins = eol.join(r.encode() for r in reqs)
            if lastreq is not None:
                text_b = text_b[:lastreq.end_byte] + eol + ins + text_b[lastreq.end_byte:]
            else:
                text_b = ins + eol + text_b
        rtb = _parse(text_b)
        tab2, ret2 = _returned(text_b, rtb)
        if ret2 is not None:
            pos = _line_start(text_b, ret2.start_byte)
            text_b = text_b[:pos] + block + eol + text_b[pos:]
        else:
            text_b = text_b.rstrip(bytes([13, 10])) + eol + eol + block
    out_files[path_b] = text_b

    # ---- a new cycle of requires through the new module would fail at load time
    graph = {}
    for p_, (s_, _r) in files.items():
        graph.setdefault(_modname(p_, root, files), set()).update(m_.group(2) for m_ in _REQ.finditer(s_.decode("utf-8", "surrogateescape")))

    def reach(g, start):
        seen, stack = set(), [start]
        while stack:
            for nx in g.get(stack.pop(), ()):
                if nx not in seen:
                    seen.add(nx)
                    stack.append(nx)
        return seen
    if mod_b not in reach(graph, mod_b):
        g2 = {k: set(v) for k, v in graph.items()}
        g2.setdefault(mod_b, set()).update({m for _a, m in carry_lines} | ({mod_a} if uses_old_table else set()))
        if kind == "global" or a_loads_b:
            g2.setdefault(mod_a, set()).add(mod_b)
        for q in updated:
            g2.setdefault(_modname(q, root, files), set()).add(mod_b)
        if mod_b in reach(g2, mod_b):
            return (f"move_symbol: the new requires would make {mod_b} and the modules around it require each other at load "
                    f"time (a Lua `require` loop); nothing was written.")

    if apply:
        for p, data in out_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    verb = "moved" if apply else "would move"
    lines = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
             f"{verb} {kind} function {name}: {mod_a} ({rel_a}) -> {mod_b} ({rel(path_b)})"]
    if stub:
        lines.append(f"{rel_a} keeps a lazy forwarding stub for {name} (methods, or a mention that could not be rewritten).")
    if updated:
        lines.append(f"files updated: {len(updated)}")
        lines += [f"  {rel(q)}" for q in updated[:12]]
    lines.append("Dynamic access (m['f'], _G), strings and non-Lua files were not rewritten.")
    if apply:
        lines += ["", "Done: the function, the requires and the callers are already changed. Do NOT Read or Edit these files "
                      "again to redo it.", "Next: run the Lua tests."]
    return "\n".join(lines)
