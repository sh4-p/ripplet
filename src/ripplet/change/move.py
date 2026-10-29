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


def _free_names(stmt) -> set:
    """Names a top-level definition or constant reads from its module."""
    loaded = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        return loaded - _BUILTINS
    # what a definition evaluates where it is defined (defaults, decorators, bases, annotations) is read from the
    # module even when a parameter of the same name shadows it inside the body
    exprs = []

    def collect(d):
        exprs.extend(d.decorator_list)
        if isinstance(d, ast.ClassDef):
            exprs.extend(d.bases)
            exprs.extend(k.value for k in d.keywords)
            for sub in d.body:                          # methods are defined while the class body runs
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    collect(sub)
        else:
            ar = d.args
            exprs.extend(ar.defaults)
            exprs.extend(x for x in ar.kw_defaults if x is not None)
            exprs.extend(x.annotation for x in ar.posonlyargs + ar.args + ar.kwonlyargs + [ar.vararg, ar.kwarg]
                         if x and x.annotation)
            if d.returns is not None:
                exprs.append(d.returns)

    collect(stmt)
    early = {n.id for e in exprs for n in ast.walk(e) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return ((loaded - _bound_inside(stmt)) | early) - _BUILTINS


def _top_level(tree: ast.Module) -> dict:
    """name -> (statement, first line including decorators) for top-level defs, classes and simple constants."""
    out, seen = {}, set()
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nm, first = st.name, min([st.lineno] + [d.lineno for d in st.decorator_list])
        elif isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            nm, first = st.targets[0].id, st.lineno
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            nm, first = st.target.id, st.lineno
        else:
            continue
        if nm in seen:
            out.pop(nm, None)                       # defined more than once: not movable on its own
            continue
        seen.add(nm)
        out[nm] = (st, first)
    return out


def move_symbol(store, cfg, symbol: str, to_file: str, apply: bool = True, with_dependencies: bool = False) -> str:
    """symbol: a top-level Python function or class (several, comma separated, move together). to_file: the module to
    move it to (created when missing). with_dependencies: also move the functions, classes and constants of the old
    module that the symbol needs, so it can leave without making the two modules import each other."""
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    wanted = [s.strip() for s in str(symbol).split(",") if s.strip()]
    if not wanted:
        return "move_symbol: pass the symbol to move."
    first, _o = _resolve(cx, wanted[0])
    ts_ext = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs")
    if first is not None and to_file and len(wanted) == 1 and _o:
        # the same name in several languages: the target file says which one is meant
        fam = lambda f: "ts" if str(f).lower().endswith(ts_ext) else Path(str(f)).suffix.lower()
        want = fam(to_file)
        if fam(first["file_path"] or "") != want:
            alt = next((n for n in _o if fam(n["file_path"] or "") == want), None)
            if alt is not None:
                first, symbol = alt, alt["id"]
                wanted = [alt["id"]]
    if first is None and wanted:      # a module-level constant is a 'variable' node, which _resolve skips
        first = next((r for r in cx.execute("SELECT * FROM nodes WHERE type='variable' AND label=?",
                                            (wanted[0].rpartition("::")[2].split(".")[-1],)).fetchall()
                      if (r["file_path"] or "").lower().endswith(ts_ext + (".go",))), None)
    if first is None and wanted[0].rpartition("::")[0].lower().endswith(".go"):
        from .move_go import move as _move_go     # a Go const is not a graph node: path::name finds it in the file
        return _move_go(store, cfg, symbol, to_file, apply, _resolve, _file, with_dependencies)
    if first is not None and (first["file_path"] or "").lower().endswith(".go"):
        from .move_go import move as _move_go
        return _move_go(store, cfg, symbol, to_file, apply, _resolve, _file, with_dependencies)
    if first is not None and (first["file_path"] or "").lower().endswith(".php"):
        from .move_php import move as _move_php
        return _move_php(store, cfg, symbol, to_file, apply, _resolve, _file)
    if first is not None and (first["file_path"] or "").lower().endswith(".cs"):
        from .move_cs import move as _move_cs
        return _move_cs(store, cfg, symbol, to_file, apply, _resolve, _file)
    if first is not None and (first["file_path"] or "").lower().endswith(".java"):
        from .move_java import move as _move_java
        return _move_java(store, cfg, symbol, to_file, apply, _resolve, _file)
    if first is not None and (first["file_path"] or "").lower().endswith(ts_ext):
        from .move_ts import move as _move_ts
        return _move_ts(store, cfg, symbol, to_file, apply, with_dependencies, _resolve, _file)
    seeds, rel_a, path_a = [], None, None
    for sym in wanted:
        node, _others = _resolve(cx, sym)
        if node is None:
            return f"move_symbol: no code symbol named {sym!r} in the index. Use search_symbols to find the exact name."
        f = (node["file_path"] or "").replace("\\", "/")
        if not f.endswith(".py"):
            return "move_symbol: only Python symbols are supported for now; this one is in " + (f or "an unknown file") + "."
        if rel_a not in (None, f):
            return "move_symbol: the symbols to move together must be in the same file."
        rel_a = f
        seeds.append((node["label"], int(node["start_line"] or 0)))
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
    top = _top_level(tree_a)
    group = []
    for nm, line in seeds:
        hit = top.get(nm)
        if hit is None or not (hit[1] <= line <= hit[0].end_lineno) \
                or isinstance(hit[0], (ast.Assign, ast.AnnAssign)):
            return (f"move_symbol: {nm} is not a top-level function or class of {rel_a} (methods and nested "
                    f"definitions can't be moved on their own); nothing was written.")
        if nm not in group:
            group.append(nm)
    name = ", ".join(group)
    eol_a = _eol(text_a)
    lines_a = text_a.split("\n")

    # what the definitions need; with_dependencies pulls in what they use from the old module
    imports_a, defs_a = _module_bindings(tree_a)
    added_deps = []
    while True:
        blockers, grow = [], []
        for nm in group:
            for f in sorted(_free_names(top[nm][0]) - {nm}):
                if f in group or f in imports_a or f not in defs_a:
                    continue
                if with_dependencies and f in top:
                    grow.append(f)
                elif f not in blockers:
                    blockers.append(f)
        grow = [g for g in dict.fromkeys(grow) if g not in group]
        if not grow:
            break
        group += grow
        added_deps += grow
    if blockers:
        hint = "" if with_dependencies else " Pass with_dependencies=true to move what it needs along, or move it first."
        return (f"move_symbol: {name} uses {', '.join(blockers)} defined in {rel_a}; moving it would make the two "
                f"modules import each other.{hint} nothing was written.")
    group.sort(key=lambda n: top[n][1])
    name = ", ".join(group)
    need_imports = {}
    for nm in group:
        for f in _free_names(top[nm][0]) - {nm}:
            if f in imports_a:
                need_imports[f] = (f,) + imports_a[f]
    need_imports = sorted(need_imports.values(), key=lambda t: (t[1].lineno, t[1].col_offset))
    seg = []
    for nm in group:
        st, first = top[nm]
        if seg:
            seg += ["", ""]
        seg += [l.rstrip("\r") for l in lines_a[first - 1:st.end_lineno]]
    new_texts: dict = {}          # path -> new text
    rewritten, attr_uses = [], {}

    # target module
    exists_b = path_b.is_file()
    text_b = _read(path_b) if exists_b else ""
    eol_b = _eol(text_b) if exists_b else eol_a
    tree_b = ast.parse(text_b) if text_b.strip() else ast.Module(body=[], type_ignores=[])
    imports_b, defs_b = _module_bindings(tree_b)
    clash = [n for n in group if n in defs_b or n in imports_b]
    if clash:
        return f"move_symbol: {path_b.name} already defines or imports {', '.join(clash)}; nothing was written."
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
    out_b += seg
    new_texts[path_b] = eol_b.join(out_b) + eol_b

    # old module: drop the definitions, then (maybe) re-export
    mod_a, mod_b = ".".join(_modname(path_a)), ".".join(_modname(path_b))
    rest = list(lines_a)
    for nm in sorted(group, key=lambda n: top[n][1], reverse=True):
        st, first = top[nm]
        del rest[first - 1:st.end_lineno]
    moved_stmts = {id(top[n][0]) for n in group}
    for st2 in tree_a.body:
        if id(st2) in moved_stmts or isinstance(st2, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
                                                       ast.ClassDef, ast.Assign, ast.AnnAssign)):
            continue
        used = sorted({x.id for x in ast.walk(st2) if isinstance(x, ast.Name) and x.id in group})
        if used:
            return (f"move_symbol: module-level code in {rel_a} (line {st2.lineno}) runs {', '.join(used)} when the module "
                    f"is imported; moving it would split that from where it is defined. Nothing was written.")
    uses_in_a = {n for n in group if any(isinstance(x, ast.Name) and x.id == n for st2 in tree_a.body
                                         if id(st2) not in moved_stmts for x in ast.walk(st2))}
    in_all = set()
    for st2 in tree_a.body:
        if isinstance(st2, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in st2.targets):
            in_all = {n for n in group if any(isinstance(c, ast.Constant) and c.value == n for c in ast.walk(st2.value))}

    # the other modules
    skip_dirs = {".venv", "venv", "node_modules", ".git", "site-packages"}
    for p in sorted(root.resolve().rglob("*.py")):
        if p in (path_a.resolve(), path_b) or any(x in skip_dirs for x in p.parts):
            continue
        try:
            txt = _read(p)
            if not any(n in txt for n in group):
                continue
            tr = ast.parse(txt)
        except (SyntaxError, OSError, ValueError):
            continue
        for n in group:
            c = _uses_attr(tr, p, mod_a, n)
            if c:
                attr_uses.setdefault(n, []).append((p, c))
        edits = [s2 for s2 in tr.body if isinstance(s2, ast.ImportFrom) and _resolve_from(s2, p) == mod_a
                 and any(a.name in group for a in s2.names)]
        if not edits:
            continue
        lines = txt.split("\n")
        eol = _eol(txt)
        for s2 in sorted(edits, key=lambda s: s.lineno, reverse=True):
            ind = " " * s2.col_offset
            mine = [al for al in s2.names if al.name in group]
            others = [al for al in s2.names if al.name not in group]
            spelled = _spell(p, mod_b, s2.level)
            moved = f"{ind}from {spelled} import " + ", ".join(
                al.name + (f" as {al.asname}" if al.asname else "") for al in mine)
            if others:
                old = "\n".join(lines[s2.lineno - 1:s2.end_lineno])
                for al in mine:
                    pat = re.escape(al.name) + r"(?:\s+as\s+\w+)?"
                    for rx in (r"(?m)^[ \t]*" + pat + r"[ \t]*,?[ \t]*\r?\n",
                               r"\b" + pat + r"\s*,\s*",
                               r",\s*\b" + pat + r"(?=\s*\)?\s*$)"):
                        cut, n_sub = re.subn(rx, "", old, count=1)
                        if n_sub:
                            old = cut
                            break
                lines[s2.lineno - 1:s2.end_lineno] = old.split("\n") + [moved]
            else:
                lines[s2.lineno - 1:s2.end_lineno] = [moved]
        new_texts[p] = eol.join(l.rstrip("\r") for l in lines)
        rewritten.append((p, len(edits)))
    keep = [n for n in group if n in uses_in_a or n in in_all or n in attr_uses]
    tmp = eol_a.join(l.rstrip("\r") for l in rest)
    if keep:
        try:
            ins = _insertion_line(ast.parse(tmp))
        except SyntaxError:
            ins = 0
        lines_t = tmp.split(eol_a)
        lines_t[ins:ins] = [f"from {mod_b} import {', '.join(keep)}  # noqa: F401"]
        tmp = eol_a.join(lines_t)
    new_texts[path_a.resolve()] = re.sub(r"(\r?\n){4,}", eol_a * 3, tmp)

    if apply:
        for p, t in new_texts.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(t.encode("utf-8", errors="surrogateescape"))
    rel = lambda p: str(Path(p).resolve().relative_to(root.resolve())).replace("\\", "/")
    verb = "moved" if apply else "would move"
    n_lines = sum(top[n][0].end_lineno - top[n][1] + 1 for n in group)
    out = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
           f"{verb} {name} ({n_lines} lines) from {rel_a} to {rel(path_b)}" + ("" if exists_b else " (new file)")]
    if added_deps:
        out.append("moved along because they need it: " + ", ".join(added_deps))
    if add_lines or tc_lines or future:
        out.append("imports added to the new module: " + "; ".join(future + add_lines + tc_lines))
    for p, n in rewritten:
        out.append(f"imports rewritten in {rel(p)}: {n}")
    if keep:
        why = [x for x, c in (("it still uses them", bool(uses_in_a)), ("it lists them in __all__", bool(in_all)),
                              ("other modules reach them as an attribute", bool(attr_uses))) if c]
        out.append(f"{rel_a} keeps `from {mod_b} import {', '.join(keep)}` because " + " and ".join(why) + ".")
    for n, items in attr_uses.items():
        for p, c in items:
            out.append(f"  {rel(p)}: {c} use(s) as `{mod_a.split('.')[-1]}.{n}` still work through that import")
    if apply:
        out += ["", "Done: the definitions, their imports and the import statements are already changed. "
                    "Do NOT Read or Edit these files again to redo it.",
                "Next: check_change to confirm nothing is left, then run the related tests."]
    return "\n".join(out)
