"""move_symbol for Ruby: move a class, module, constant or top-level method to another file.

Ruby constants are global: a class keeps its name wherever its file is, and nobody imports it by path. What changes is
which file *loads* it. So a move is:

* the definition (with its doc comments, and the `module` / `class` lines around it when it was nested) is cut out and
  put in the target file under the same wrappers, so the constant path stays `App::Foo`;
* the cut leaves `require_relative 'new'` at the same spot of the old file, so whatever required the old file still gets
  the class and the load order inside the old file is unchanged (a wrapper that would hold only that line is dropped);
* the new file gets the old file's `require` / `require_relative` lines (paths re-based) and `require_relative 'old'`
  when the moved code names a constant the old file defines.
In a Zeitwerk / Rails project (autoload by file name) no `require` lines are written and the new file must be named after
the constant. It refuses code that depends on its own file location (`__FILE__`, `__dir__`, `require_relative`) when the
directory changes, a constant defined more than once in the file, a name that `autoload` points at, and a target that
already defines the name.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP = {"node_modules", ".git", "vendor", "tmp", "log", "coverage", ".bundle"}
_parser = None
_REQ = ("require", "require_relative", "autoload", "load")


def _parse(src: bytes):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_ruby as tr
        _parser = ts.Parser(ts.Language(tr.language()))
    return _parser.parse(src).root_node


def _t(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="surrogateescape")


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _rb_files(root: Path) -> list:
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in _SKIP and not d.startswith("."))
        out += [Path(dp) / f for f in sorted(fn) if f.endswith(".rb")]
    return out


def _cname(src: bytes, n) -> str:
    x = n.child_by_field_name("name")
    return _t(src, x) if x is not None else ""


def _body(n):
    return next((c for c in n.children if c.type == "body_statement"), None)


def _defs(src: bytes, root) -> list:
    """(last name, node, wrapper chain, kind) for classes, modules, constants and top-level methods."""
    out = []

    def visit(children, chain):
        for c in children:
            if c.type in ("class", "module"):
                nm = _cname(src, c)
                out.append((nm.split("::")[-1], c, chain, c.type))
                b = _body(c)
                if b is not None and "::" not in nm:
                    visit(b.children, chain + (c,))
            elif c.type == "method" and not chain:
                nm = c.child_by_field_name("name")
                out.append((_t(src, nm), c, chain, "def"))
            elif c.type == "assignment":
                left = c.child_by_field_name("left")
                if left is not None and left.type == "constant":
                    out.append((_t(src, left), c, chain, "const"))
    visit(root.children, ())
    return out


def _doc_start(src: bytes, n) -> int:
    start, prev = n.start_byte, n.prev_sibling
    while prev is not None and prev.type == "comment" and src[prev.end_byte:start].count(b"\n") <= 1:
        start, prev = prev.start_byte, prev.prev_sibling
    return start


def _line_start(src: bytes, pos: int) -> int:
    return src.rfind(b"\n", 0, pos) + 1


def _header_end(src: bytes, w) -> int:
    end = w.start_byte
    for c in w.children:
        if c.type in ("body_statement", "end", "comment"):
            if c.type == "body_statement":
                break
            continue
        end = max(end, c.end_byte)
    return end


def _autoload_mode(root: Path) -> bool:
    for name in ("Gemfile", "Gemfile.lock"):
        p = root / name
        if p.exists():
            t = p.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\b(rails|zeitwerk)\b", t):
                return True
    return False


def _snake(name: str) -> str:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s).lower()


def _reach(start: Path, override: dict) -> set:
    """Files the start file loads through `require_relative`, directly or not (start excluded)."""
    seen, queue = {start}, [start]
    while queue:
        cur = queue.pop()
        try:
            data = override[cur] if cur in override else cur.read_bytes()
        except OSError:
            continue
        for m in re.finditer(rb"require_relative\s*\(?\s*['\"](.+?)['\"]", data):
            tgt = (cur.parent / m.group(1).decode()).with_suffix(".rb")
            if tgt.exists() and tgt not in seen:
                seen.add(tgt)
                queue.append(tgt)
    return seen - {start}


def _rel_require(frm: Path, to: Path) -> str:
    r = os.path.relpath(to.with_suffix(""), frm.parent).replace("\\", "/")
    return r


def _requires(src: bytes, root) -> list:
    out = []
    for c in root.children:
        if c.type == "call":
            m = c.child_by_field_name("method")
            if m is not None and _t(src, m) in _REQ and c.child_by_field_name("receiver") is None:
                out.append(c)
    return out


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: Ruby definitions move one at a time; pass one symbol."
    node, _o = resolve(cx, symbol.strip())
    if node is None:
        where, _s, nm0 = symbol.strip().rpartition("::")
        if where.endswith(".rb"):
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
    if path_b.suffix != ".rb":
        return "move_symbol: to_file must be a .rb file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."

    src_a = path_a.read_bytes()
    root_a = _parse(src_a)
    name = re.split(r"::|\.|#", node["label"])[-1]
    defs = _defs(src_a, root_a)
    hits = [d for d in defs if d[0] == name]
    if not hits:
        return (f"move_symbol: {name} is not a class, module, constant or top-level method defined in {rel_a} "
                f"(members can't be moved on their own); nothing was written.")
    if len(hits) > 1:
        return f"move_symbol: {name} is defined {len(hits)} times in {rel_a}; nothing was written."
    _n, node_d, chain, kind = hits[0]
    text_d = _t(src_a, node_d)
    same_dir = path_b.parent == path_a.parent
    if not same_dir and re.search(r"__FILE__|__dir__|\brequire_relative\b|\bautoload\b", text_d):
        return (f"move_symbol: {name} depends on its file location (__FILE__, __dir__, require_relative or autoload) and the "
                f"target is in another directory; nothing was written.")
    zeit = _autoload_mode(root)
    if zeit and kind in ("class", "module") and path_b.stem != _snake(name):
        return (f"move_symbol: this project autoloads by file name; the new file must be named {_snake(name)}.rb. "
                f"Nothing was written.")

    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")
    for p in _rb_files(root):
        s = src_a if p == path_a else p.read_bytes()
        if re.search(rb"\bautoload\s+:" + re.escape(name.encode()) + rb"\b", s):
            return (f"move_symbol: {rel(p)} autoloads {name} by path; update that path by hand or move the file instead. "
                    f"Nothing was written.")
    src_b0 = path_b.read_bytes() if path_b.exists() else None
    root_b = _parse(src_b0) if src_b0 is not None else None
    if root_b is not None and any(d[0] == name for d in _defs(src_b0, root_b)):
        return f"move_symbol: {path_b.name} already defines {name}; nothing was written."
    eol = b"\r\n" if b"\r\n" in src_a else b"\n"

    # ---- the cut
    doc = _doc_start(src_a, node_d)
    if node_d.prev_sibling is None and node_d.parent is not None and node_d.parent.type == "body_statement":
        prev, start = node_d.parent.prev_sibling, doc
        while prev is not None and prev.type == "comment" and src_a[prev.end_byte:start].count(b"\n") <= 1:
            start, prev = prev.start_byte, prev.prev_sibling
        doc = start
    cut_from = _line_start(src_a, doc)
    indent = src_a[cut_from:doc]
    piece = src_a[doc:node_d.end_byte]
    # wrappers that would hold nothing else collapse into the require line
    outer = node_d
    for w in reversed(chain):
        b = _body(w)
        kids = [c for c in (b.children if b is not None else []) if c.type != "comment"]
        if len(kids) == 1 and kids[0] == outer:
            outer = w
        else:
            break
    collapsed = outer is not node_d
    cut_start = _line_start(src_a, _doc_start(src_a, outer) if collapsed else doc)
    cut_end = outer.end_byte
    while src_a[cut_end:cut_end + 1] in (b" ", bytes([9])):
        cut_end += 1
    if src_a[cut_end:cut_end + 2] == b"\r\n":
        cut_end += 2
    elif src_a[cut_end:cut_end + 1] == b"\n":
        cut_end += 1
    lead = src_a[cut_start:cut_start + (len(src_a[cut_start:]) - len(src_a[cut_start:].lstrip(b" \t")))]
    req_line = b"" if zeit else lead + f"require_relative '{_rel_require(path_a, path_b)}'".encode() + eol
    new_a = src_a[:cut_start] + req_line + src_a[cut_end:]

    # ---- what the moved code names from the old file
    consts = {c for c in re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", text_d)}
    left_defs = {d[0] for d in defs if d[0] != name and d[3] != "def"}
    names_a = set(re.findall(r"\b[a-z_]\w*\b", text_d))
    left_meths = {d[0] for d in defs if d[3] == "def" and d[0] != name}
    needs_old = bool((consts & left_defs) or (names_a & left_meths))

    if not zeit and needs_old and src_b0 is not None and path_b in _reach(path_a, {path_a: src_a}):
        return (f"move_symbol: {rel_a} loads {path_b.name} (through its requires) before it defines what {name} needs, "
                f"so {name} can't live in {path_b.name} without a circular load. Nothing was written.")

    # ---- the target
    wrapper_open = [src_a[w.start_byte:_header_end(src_a, w)] for w in chain]
    pad = b"".join(wrapper_open[i] + eol for i in range(len(chain)))
    # the moved text keeps its indentation, so every wrapper line goes where it was
    def wrapped() -> bytes:
        out = b""
        for i, w in enumerate(chain):
            ws = _line_start(src_a, w.start_byte)
            out += src_a[ws:_header_end(src_a, w)] + eol
        out += indent + piece + eol
        for w in reversed(chain):
            ws = _line_start(src_a, w.start_byte)
            ind = src_a[ws:ws + (len(src_a[ws:]) - len(src_a[ws:].lstrip(b" \t")))]
            out += ind + b"end" + eol
        return out
    block = wrapped()
    req_a = _requires(src_a, root_a)
    carried = []
    for c in req_a:
        line = _t(src_a, c)
        m = re.match(r"require_relative\s*\(?\s*(['\"])(.+?)\1", line)
        if m:
            target = (path_a.parent / m.group(2)).resolve()
            line = f"require_relative '{_rel_require(path_b, target)}'" if not same_dir else line
        carried.append(line)
    if needs_old and not zeit:
        carried.append(f"require_relative '{_rel_require(path_b, path_a)}'")
    if src_b0 is None:
        magic = [ln for ln in src_a.decode("utf-8", "surrogateescape").splitlines()[:3] if re.match(r"#\s*(frozen_string_literal|encoding|typed|-\*-)", ln)]
        head = ("\n".join(magic) + ("\n\n" if magic else "")) if magic else ""
        reqs = ("\n".join(dict.fromkeys(carried)) + "\n\n") if carried and not zeit else ""
        text_b = (head + reqs).encode("utf-8", "surrogateescape").replace(b"\n", eol) + block
    else:
        have = {_t(src_b0, c) for c in _requires(src_b0, root_b)}
        add = list(dict.fromkeys(carried)) if not zeit else []     # requires are idempotent: repeat them above the block
        # Placement. If a file the target requires (directly or not) uses the moved name, the definition has to come before
        # the target's own requires; otherwise it goes last, after the definitions it may rely on. Both at once is a cycle.
        seen_f, queue, early = {path_b}, [path_b], False
        name_rx = re.compile(rb"\b" + re.escape(name.encode()) + rb"\b")
        while queue:
            cur = queue.pop()
            data_c = src_b0 if cur == path_b else (new_a if cur == path_a else cur.read_bytes())
            for m in re.finditer(rb"require_relative\s*\(?\s*['\"](.+?)['\"]", data_c):
                tgt = (cur.parent / m.group(1).decode()).with_suffix(".rb")
                if tgt.exists() and tgt not in seen_f:
                    seen_f.add(tgt)
                    queue.append(tgt)
                    body_t = new_a if tgt == path_a else tgt.read_bytes()
                    if name_rx.search(body_t):
                        early = True
        for x in _walk(root_b):          # the target's own load-time code (outside method bodies) names it
            if x.type == "constant" and _t(src_b0, x) == name:
                q = x.parent
                while q is not None and q.type not in ("method", "singleton_method"):
                    q = q.parent
                if q is None:
                    early = True
                    break
        defs_b = {d[0] for d in _defs(src_b0, root_b) if d[3] != "def"}
        if early and (consts & defs_b):
            return (f"move_symbol: {path_b.name} and the files it requires need {name} while {name} needs "
                    f"{', '.join(sorted(consts & defs_b)[:3])} from {path_b.name}: a circular load. Nothing was written.")
        ins = (eol.join(a_.encode() for a_ in dict.fromkeys(carried)) + eol + eol) if carried and not zeit else b""
        first = next((c for c in root_b.children if c.type != "comment"), None)
        if early and first is not None:
            pos = _line_start(src_b0, first.start_byte)
            text_b = src_b0[:pos] + ins + block + eol + src_b0[pos:]
        else:
            text_b = src_b0.rstrip(bytes([13, 10])) + eol + eol + ins + block

    drop_a = zeit and not [c for c in _parse(new_a).children if c.type not in ("comment",) and c not in _requires(new_a, _parse(new_a))]
    if apply:
        path_b.parent.mkdir(parents=True, exist_ok=True)
        path_b.write_bytes(text_b)
        if drop_a:
            path_a.unlink()
        else:
            path_a.write_bytes(new_a)
    verb = "moved" if apply else "would move"
    lines = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
             f"{verb} {kind} {name}: {rel_a} -> {rel(path_b)}" + (f" (inside {' / '.join(_cname(src_a, w) for w in chain)})" if chain else "")]
    if zeit:
        lines.append("Autoload project: no require lines written." + (f" {rel_a} had nothing else and was deleted." if drop_a else ""))
    else:
        lines.append(f"{rel_a} now has `require_relative '{_rel_require(path_a, path_b)}'` where it stood, so everything that "
                     f"required it still gets {name}." + (" The wrapper around it was dropped." if collapsed else ""))
        if needs_old:
            lines.append(f"{path_b.name} requires {path_a.name} back: the moved code names constants or methods defined there.")
    lines.append("String-built constant names, `Object.const_get` and non-Ruby files were not checked.")
    if apply:
        lines += ["", "Done: the definition and the require lines are already changed. Do NOT Read or Edit these files again "
                      "to redo it.", "Next: run the specs / load the files."]
    return "\n".join(lines)
