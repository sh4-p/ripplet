"""move_symbol: move a top-level Python function or class to another module and repair the imports.

Moving a symbol by hand costs a turn per file: cut it, paste it, copy the imports it needs, fix every
`from old import name`. This does it in one call from the syntax tree of each file (not the graph's guesses),
all files computed first and written together, or nothing written:

* the definition (decorators included) leaves the old module and lands at the end of the new one, together with
  the imports it needs (an import the new module already has is not repeated);
* `from old import name` statements elsewhere now name the new module;
* the old module keeps `from new import name` when it still uses the name, or when some module reaches it as
  `old.name` (an import that can't be rewritten safely), so nothing breaks;
* it refuses, writing nothing, when the symbol uses a name defined in the old module (that would make the two
  modules import each other), when the target already defines the name, or when the symbol is nested.
"""
from __future__ import annotations

import ast
import builtins
import re
from pathlib import Path

from .plan import _resolve
from .rename import _file

_BUILTINS = set(dir(builtins))


def _modname(path: Path) -> list:
    """Dotted module path of a file: its stem under every parent directory that is a package."""
    parts = [] if path.stem == "__init__" else [path.stem]
    d = path.parent
    while (d / "__init__.py").is_file():
        parts.insert(0, d.name)
        d = d.parent
    return parts


def _resolve_from(node: ast.ImportFrom, here: Path) -> str | None:
    """Absolute dotted module an `ImportFrom` statement names, or None when it can't be worked out."""
    if not node.level:
        return node.module or ""
    pkg = _modname(here)
    if here.stem != "__init__":
        pkg = pkg[:-1]
    if node.level - 1 > len(pkg):
        return None
    base = pkg[:len(pkg) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _spell(here: Path, target: str, level: int) -> str:
    """The module `target` as `here` should write it in a `from` import: relative when the old import was."""
    if not level:
        return target
    pkg = _modname(here) if here.stem == "__init__" else _modname(here)[:-1]
    tp = target.split(".")
    common = 0
    while common < min(len(pkg), len(tp)) and pkg[common] == tp[common]:
        common += 1
    if common == 0 or common == len(tp):
        return target
    return "." * (len(pkg) - common + 1) + ".".join(tp[common:])


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8", errors="surrogateescape")


def _bound_inside(fn: ast.AST) -> set:
    """Names the definition binds itself (parameters, assignments, loop and with targets, nested defs, imports)."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
            out.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and getattr(n, "name", None):
            out.add(n.name)
    return out


def _module_bindings(tree: ast.Module) -> tuple:
    """(imports, defs): name -> import statement node / name -> line of a top-level def, class or assignment."""
    imports, defs = {}, {}

    def nested(body, guard):
        for sub in body:
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for a in sub.names:
                    if a.name != "*":
                        key = (a.asname or a.name).split(".")[0]
                        # a name bound in more than one branch (TYPE_CHECKING / else / except) is conditional
                        imports[key] = (sub, a, "other" if key in imports else guard)
            elif isinstance(sub, ast.If):
                tc = ast.unparse(sub.test) in ("TYPE_CHECKING", "typing.TYPE_CHECKING")
                nested(sub.body, "tc" if tc else "other")
                nested(sub.orelse, "other")
            elif isinstance(sub, ast.Try):
                for blk in (sub.body, sub.orelse, sub.finalbody, *[h.body for h in sub.handlers]):
                    nested(blk, "other")

    for st in tree.body:
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            for a in st.names:
                if a.name == "*":
                    continue
                imports[(a.asname or a.name).split(".")[0]] = (st, a, None)
        elif isinstance(st, (ast.If, ast.Try)):
            nested([st], None)
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[st.name] = st.lineno
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        defs[n.id] = st.lineno
    return imports, defs


def _import_text(st, alias, here: Path, there: Path) -> str | None:
    """One import statement for a single alias, spelled so it works from `there` (None if it can't be)."""
    asname = f" as {alias.asname}" if alias.asname else ""
    if isinstance(st, ast.Import):
        return f"import {alias.name}{asname}"
    if st.level:
        if here.parent != there.parent:
            return None
        return f"from {'.' * st.level}{st.module or ''} import {alias.name}{asname}"
    return f"from {st.module} import {alias.name}{asname}"


def _insertion_line(tree: ast.Module) -> int:
    """0-based line index after the last top-level import (or the docstring), where new imports go."""
    end = 0
    for st in tree.body:
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            end = st.end_lineno
        elif end == 0 and isinstance(st, ast.Expr) and isinstance(getattr(st, "value", None), ast.Constant) \
                and isinstance(st.value.value, str):
            end = st.end_lineno
        elif end:
            break
    return end


def _uses_attr(tree: ast.Module, here: Path, mod_a: str, name: str) -> int:
    """How many times this module reaches `name` as an attribute of the old module (`old.name`, `pkg.old.name`)."""
    bound = set()
    for st in tree.body:
        if isinstance(st, ast.Import):
            for a in st.names:
                if a.name == mod_a:
                    bound.add(a.asname or a.name)
        elif isinstance(st, ast.ImportFrom):
            base = _resolve_from(st, here)
            if base is None:
                continue
            for a in st.names:
                if (base + "." + a.name).strip(".") == mod_a:
                    bound.add(a.asname or a.name)
    if not bound:
        return 0
    n = 0
    for x in ast.walk(tree):
        if isinstance(x, ast.Attribute) and x.attr == name:
            parts = []
            v = x.value
            while isinstance(v, ast.Attribute):
                parts.insert(0, v.attr)
                v = v.value
            if isinstance(v, ast.Name):
                parts.insert(0, v.id)
                if ".".join(parts) in bound:
                    n += 1
    return n


def move_symbol(store, cfg, symbol: str, to_file: str, apply: bool = True) -> str:
    """symbol: a top-level Python function or class. to_file: the module to move it to (created when missing)."""
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    node, _others = _resolve(cx, symbol)
    if node is None:
        return f"move_symbol: no code symbol named {symbol!r} in the index. Use search_symbols to find the exact name."
    name = node["label"]
    rel_a = (node["file_path"] or "").replace("\\", "/")
    if not rel_a.endswith(".py"):
        return "move_symbol: only Python symbols are supported for now; this one is in " + (rel_a or "an unknown file") + "."
    path_a = _file([str(r) for r in roots], rel_a)
    if path_a is None:
        return f"move_symbol: {rel_a} is not on disk (index out of date?)."
    root = next((r for r in roots if str(path_a).startswith(str(r.resolve())) or str(path_a).startswith(str(r))), roots[0])
    path_b = Path(to_file)
    if not path_b.is_absolute():
        path_b = root / to_file
    path_b = path_b.resolve()
    if path_b.suffix != ".py":
        return "move_symbol: to_file must be a .py file."
    try:
        path_b.relative_to(root.resolve())
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a.resolve():
        return "move_symbol: the symbol is already in that file."

    text_a = _read(path_a)
    try:
        tree_a = ast.parse(text_a)
    except SyntaxError as e:
        return f"move_symbol: {rel_a} does not parse ({e.msg}, line {e.lineno}); nothing was written."
    start_line = int(node["start_line"] or 0)
    target = None
    for st in tree_a.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and st.name == name:
            first = min([st.lineno] + [d.lineno for d in st.decorator_list])
            if first <= start_line <= st.end_lineno:
                target = (st, first)
    if target is None:
        return (f"move_symbol: {name} is not a top-level function or class of {rel_a} (methods and nested "
                f"definitions can't be moved on their own); nothing was written.")
    st, first = target
    eol_a = _eol(text_a)
    lines_a = text_a.split("\n")
    seg = "\n".join(l.rstrip("\r") for l in lines_a[first - 1:st.end_lineno])

    # what the definition needs
    imports_a, defs_a = _module_bindings(tree_a)
    inner = _bound_inside(st)
    loaded = {n.id for n in ast.walk(st) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    free = sorted(loaded - inner - _BUILTINS - {name})
    need_imports, blockers = [], []
    for nm in free:
        if nm in imports_a:
            need_imports.append((nm,) + imports_a[nm])
        elif nm in defs_a:
            blockers.append(nm)
    need_imports.sort(key=lambda t: (t[1].lineno, t[1].col_offset))
    if blockers:
        return (f"move_symbol: {name} uses {', '.join(blockers)} defined in {rel_a}; moving it would make the two "
                f"modules import each other. Move {'it' if len(blockers) == 1 else 'them'} first (or together); "
                f"nothing was written.")
    new_texts: dict = {}          # path -> new text
    notes, rewritten, attr_uses = [], [], []

    # target module
    exists_b = path_b.is_file()
    text_b = _read(path_b) if exists_b else ""
    eol_b = _eol(text_b) if exists_b else eol_a
    tree_b = ast.parse(text_b) if text_b.strip() else ast.Module(body=[], type_ignores=[])
    imports_b, defs_b = _module_bindings(tree_b)
    if name in defs_b or name in imports_b:
        return f"move_symbol: {path_b.name} already defines or imports {name}; nothing was written."
    add_lines, tc_lines = [], []
    for nm, ist, alias, guard in need_imports:
        if nm in imports_b:
            continue
        if nm in defs_b:
            return f"move_symbol: {path_b.name} defines {nm}, which {name} needs from an import; nothing was written."
        if guard == "other":
            return (f"move_symbol: {name} needs {nm}, which {rel_a} imports conditionally (try/except or an if); "
                    f"nothing was written.")
        t = _import_text(ist, alias, path_a, path_b)
        if t is None:
            return (f"move_symbol: {name} needs the relative import of {nm}, and {path_b.name} is in another "
                    f"directory; nothing was written.")
        (tc_lines if guard == "tc" else add_lines).append(t)
    if tc_lines and "TYPE_CHECKING" not in imports_b:
        add_lines.append("from typing import TYPE_CHECKING")

    def has_future(tree):
        return any(isinstance(x, ast.ImportFrom) and x.module == "__future__" for x in tree.body)

    future = []
    if has_future(tree_a) and not has_future(tree_b):
        future = ["from __future__ import annotations"]
    lines_b = text_b.split("\n") if text_b else []
    at = _insertion_line(tree_b) if exists_b else 0
    doc_end = 0
    if tree_b.body and isinstance(tree_b.body[0], ast.Expr) and isinstance(getattr(tree_b.body[0], "value", None), ast.Constant) \
            and isinstance(tree_b.body[0].value.value, str):
        doc_end = tree_b.body[0].end_lineno
    head = [l.rstrip("\r") for l in lines_b[:at]]
    tail = [l.rstrip("\r") for l in lines_b[at:]]
    while tail and not tail[-1].strip():
        tail.pop()
    if future:
        head = head[:doc_end] + future + ([""] if len(head) > doc_end or add_lines or tc_lines else []) + head[doc_end:]
    block = (["", "if TYPE_CHECKING:"] + ["    " + t for t in tc_lines]) if tc_lines else []
    out_b = head + add_lines + block + tail
    while out_b and not out_b[0].strip():
        out_b.pop(0)
    if out_b:
        out_b += ["", ""]
    out_b += seg.split("\n")
    new_texts[path_b] = eol_b.join(out_b) + eol_b

    # old module: drop the definition, then (maybe) re-export
    mod_a, mod_b = ".".join(_modname(path_a)), ".".join(_modname(path_b))
    rest = lines_a[:first - 1] + lines_a[st.end_lineno:]
    uses_in_a = any(isinstance(n, ast.Name) and n.id == name for st2 in tree_a.body if st2 is not st
                    for n in ast.walk(st2))
    in_all = False
    for st2 in tree_a.body:
        if isinstance(st2, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in st2.targets):
            in_all = any(isinstance(c, ast.Constant) and c.value == name for c in ast.walk(st2.value))

    # the other modules
    for p in sorted(root.resolve().rglob("*.py")):
        if p in (path_a.resolve(), path_b) or any(x in {".venv", "venv", "node_modules", ".git", "site-packages"} for x in p.parts):
            continue
        try:
            txt = _read(p)
            if name not in txt:
                continue
            tr = ast.parse(txt)
        except (SyntaxError, OSError, ValueError):
            continue
        n_attr = _uses_attr(tr, p, mod_a, name)
        if n_attr:
            attr_uses.append((p, n_attr))
        edits = []
        for s2 in tr.body:
            if isinstance(s2, ast.ImportFrom) and _resolve_from(s2, p) == mod_a and any(a.name == name for a in s2.names):
                edits.append(s2)
        if not edits:
            continue
        lines = txt.split("\n")
        eol = _eol(txt)
        for s2 in sorted(edits, key=lambda s: s.lineno, reverse=True):
            ind = " " * s2.col_offset
            mine = [al for al in s2.names if al.name == name]
            others = [al for al in s2.names if al.name != name]
            spelled = _spell(p, mod_b, s2.level)
            moved = f"{ind}from {spelled} import " + ", ".join(
                al.name + (f" as {al.asname}" if al.asname else "") for al in mine)
            if others:
                old = "\n".join(lines[s2.lineno - 1:s2.end_lineno])
                pat = re.escape(name) + r"(?:\s+as\s+\w+)?"
                for rx in (r"(?m)^[ \t]*" + pat + r"[ \t]*,?[ \t]*\r?\n",
                           r"\b" + pat + r"\s*,\s*",
                           r",\s*\b" + pat + r"(?=\s*\)?\s*$)"):
                    cut, n_sub = re.subn(rx, "", old, count=1)
                    if n_sub:
                        break
                lines[s2.lineno - 1:s2.end_lineno] = cut.split("\n") + [moved]
            else:
                lines[s2.lineno - 1:s2.end_lineno] = [moved]
        new_texts[p] = eol.join(l.rstrip("\r") for l in lines)
        rewritten.append((p, len(edits)))
    keep = uses_in_a or in_all or bool(attr_uses)
    if keep:
        mod_text = f"from {mod_b} import {name}  # noqa: F401"
        # place it after A's last import, computed on the text without the definition
        tmp = eol_a.join(l.rstrip("\r") for l in rest)
        try:
            tr2 = ast.parse(tmp)
            ins = _insertion_line(tr2)
        except SyntaxError:
            ins = 0
        lines_t = tmp.split(eol_a)
        lines_t[ins:ins] = [mod_text]
        rest_text = eol_a.join(lines_t)
    else:
        rest_text = eol_a.join(l.rstrip("\r") for l in rest)
    rest_text = re.sub(r"(\r?\n){4,}", eol_a * 3, rest_text)
    new_texts[path_a.resolve()] = rest_text

    if apply:
        for p, t in new_texts.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(t.encode("utf-8", errors="surrogateescape"))
    rel = lambda p: str(Path(p).resolve().relative_to(root.resolve())).replace("\\", "/")
    verb = "moved" if apply else "would move"
    out = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
           f"{verb} {name} ({st.end_lineno - first + 1} lines) from {rel_a} to {rel(path_b)}" +
           ("" if exists_b else " (new file)")]
    if add_lines or tc_lines or future:
        out.append("imports added to the new module: " + "; ".join(future + add_lines + tc_lines))
    for p, n in rewritten:
        out.append(f"imports rewritten in {rel(p)}: {n}")
    if keep:
        why = [x for x, c in (("it still uses it", uses_in_a), ("it lists it in __all__", in_all),
                              ("other modules reach it as an attribute", bool(attr_uses))) if c]
        out.append(f"{rel_a} keeps `from {mod_b} import {name}` because " + " and ".join(why) + ".")
    for p, n in attr_uses:
        out.append(f"  {rel(p)}: {n} use(s) as `{mod_a.split('.')[-1]}.{name}` still work through that import")
    if apply:
        out += ["", "Done: the definition, its imports and the import statements are already changed. "
                    "Do NOT Read or Edit these files again to redo it.",
                "Next: check_change to confirm nothing is left, then run the related tests."]
    return "\n".join(out)
