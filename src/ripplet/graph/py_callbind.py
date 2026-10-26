"""Import- and heritage-aware Python call binding + superseded-unresolved table.

Runs once per index/update on the *published* graph (after sweep, before
commit) and is a pure function of the final edge/node tables, so an
incremental update and a full rebuild produce identical rows.

It replaces the old repo-wide same-name guess (`name-resolver`) for Python:
a call binds only when the receiver's meaning is established by evidence
already in the graph:

  f()          `f` is bound in the caller's module by a resolved import
               (`from x import f`, `from x import f as g`).
  mod.f()      `mod` is a resolved module import/alias; `f` is defined in, or
               re-exported by (bounded chain), that module.
  C.m()        `C` is an imported class; `m` found on C or its bases.
  self.m()     `m` found on the enclosing class's bases across files (same-
  cls.m()      file bases are already bound by the extractor).
  super().m()  bases only, never the calling class.
  p.m()        `p` is a parameter annotated with a class (`p: C`,
               `Optional[C]`, `C | None`); m found on C or its bases.

Everything else (attribute/local/parameter receivers, star imports,
builtins) stays `calls_unresolved`.

The second product is `superseded_edges`: every `calls_unresolved` row whose
call site is now covered by a resolved `calls`/`instantiates` edge (same
source, same line, target label == callee name). Readers hide those rows so
the graph never states both "resolved" and "unresolved" for one call.
"""
from __future__ import annotations

import ast
import collections
import json
from pathlib import Path

from ..core.identifiers import canonical_edge_id, site_fingerprint
from ..core.models import Edge

METHOD = "py-call-bind"
VERSION = "15"  # v13: `with` enters __enter__/__exit__, getattr(self, "prefix" + x) dispatch; v12: conditional collection attributes, locals from typed dict/list subscripts; v11: properties, collection element types in for-loops, module globals; v10: calling a typed object types the result by its __call__; v9: unannotated receivers typed by the repo's naming convention (inferred); v8: locals of nested defs do not leak into the enclosing function; v7: rows of one call spelling merge their sites into one edge (last row won); v6: unique distinctive method names bind untyped receivers (inferred); v5: locals typed through a typed param's method return; v4: attribute chains on typed params; v3: local/loop variable types; v2: self-attribute and pytest-fixture receivers
_CHAIN = 4   # re-export hops followed through module import maps
_MRO = 16    # classes visited per heritage walk

SUPERSEDED_DDL = ("CREATE TABLE IF NOT EXISTS superseded_edges("
                  "edge_id TEXT PRIMARY KEY, by_edge TEXT)")


def _j(s):
    try:
        return json.loads(s or "{}") or {}
    except Exception:
        return {}


def bind_and_supersede(store, repo_roots: dict | None = None) -> dict:
    cx = store.cx
    cx.execute(SUPERSEDED_DDL)
    # 1. drop previous derivation (pure recompute)
    old = [r[0] for r in cx.execute("SELECT id FROM edges WHERE method=?", (METHOD,))]
    for i in range(0, len(old), 500):
        ch = old[i:i + 500]
        q = ",".join("?" * len(ch))
        cx.execute(f"DELETE FROM edges WHERE id IN ({q})", ch)
        cx.execute(f"DELETE FROM file_edges WHERE edge_id IN ({q})", ch)
    cx.execute("DELETE FROM superseded_edges")

    # 2. python symbol tables
    by_q: dict[str, str] = {}
    ntype: dict[str, str] = {}
    qname: dict[str, str] = {}
    mod_of_file: dict[str, str] = {}
    classes: set[str] = set()
    nfile: dict[str, str] = {}
    nline: dict[str, int] = {}
    for r in cx.execute("SELECT id, type, qualified_name, file_path, attributes, start_line "
                        "FROM nodes WHERE language='python'"):
        nid, t, qn, fp = r[0], r[1], r[2] or "", r[3] or ""
        nfile[nid], nline[nid] = fp, r[5] or 0
        ntype[nid] = t
        qname[nid] = qn
        if t == "module":
            mod_of_file[fp] = nid
            by_q.setdefault(qn, nid)
        elif t in ("function", "method", "class", "test", "constant"):
            prev = by_q.get(qn)
            # `@overload` stubs come first; the implementation is the last def
            if prev is None or (t in ("function", "method") and (r[5] or 0) > nline.get(prev, 0)):
                by_q[qn] = nid
            if t == "class" or (t == "test" and _j(r[4]).get("test_kind") == "suite"):
                classes.add(qn)

    # 3. import maps: binding scope (module, or the function/class whose
    # body holds a local import) -> local name -> target node id
    imap: dict[str, dict[str, str]] = {}
    bound_in: dict[tuple[str, str], list[str]] = {}
    for src, tgt, a in cx.execute(
            "SELECT source, target, attributes FROM edges WHERE relation='imports' "
            "AND method='python-ast'"):
        sc = _j(a).get("bound_in")
        if sc:
            bound_in[(src, tgt)] = list(sc)

    def holders(src: str, ext_target: str) -> list[str]:
        sc = bound_in.get((src, ext_target))
        if not sc:
            return [src]
        return [h for h in (by_q.get(q) for q in sc) if h]

    # `@overload` stubs + implementation share one name: an import of it is
    # "ambiguous" between them, but the implementation (last def) is meant
    ambiguous_pick: dict = {}
    for src, tgt, a in cx.execute(
            "SELECT source, target, attributes FROM edges WHERE relation='imports' "
            "AND method='py-import-resolve'"):
        d = _j(a)
        if d.get("resolution") == "ambiguous" and tgt in ntype:
            key = (src, d.get("original_target") or "")
            cur = ambiguous_pick.get(key)
            if cur is None or (qname.get(cur) == qname.get(tgt) and nline.get(tgt, 0) > nline.get(cur, 0)):
                ambiguous_pick[key] = tgt if cur is None or qname.get(cur) == qname.get(tgt) else cur
    rows = list(cx.execute("SELECT source, target, attributes FROM edges WHERE relation='imports' "
                           "AND method='py-import-resolve'"))
    for (src, ot), tgt in ambiguous_pick.items():
        same = {r[1] for r in rows if r[0] == src and _j(r[2]).get("original_target") == ot}
        if len({qname.get(x) for x in same}) == 1:     # only overloads of one name
            d = next(_j(r[2]) for r in rows if r[0] == src and r[1] == tgt)
            rows.append((src, tgt, json.dumps(dict(d, resolution="resolved"))))
    for src, tgt, a in rows:
        d = _j(a)
        if d.get("resolution") != "resolved" or tgt not in ntype:
            continue
        imported, alias = d.get("imported") or "", d.get("alias") or ""
        if not imported:
            continue
        if ntype[tgt] == "module":
            if alias:
                local = alias
            elif "." not in imported:
                local = imported
            else:
                continue  # `import a.b` binds `a`, not this module
        else:
            local = alias or imported.rsplit(".", 1)[-1]
        for h in holders(src, d.get("original_target") or ""):
            imap.setdefault(h, {}).setdefault(local, tgt)
    # `from pkg import sub [as s]` where `sub` is a submodule, not a symbol
    # of pkg/__init__: py-import-resolve reports it unresolved, but the
    # from-module edge resolved `pkg`, and `pkg.sub` is a real module node.
    # Keyed by (module, from_module, level): one from-module edge is merged
    # per package, so its `original_target` names only the first import.
    pkg_of: dict[tuple, str] = {}
    for src, tgt, a in cx.execute(
            "SELECT source, target, attributes FROM edges WHERE relation='imports' "
            "AND method='py-module-import'"):
        d = _j(a)
        if d.get("resolution") == "resolved" and d.get("via") == "from-module":
            pkg_of[(src, d.get("from_module") or "", int(d.get("level") or 0))] = tgt
    for src, tgt, a in cx.execute(
            "SELECT source, target, attributes FROM edges WHERE relation='imports' "
            "AND method='python-ast'"):
        d = _j(a)
        pkg = pkg_of.get((src, d.get("from_module") or "", int(d.get("level") or 0)))
        if not pkg or not d.get("from_module") and not d.get("level"):
            continue
        names = d.get("names") or []
        if len(names) != 1 or not isinstance(names[0], str):
            continue
        local = d.get("alias") or names[0]
        sub = by_q.get(f"{qname.get(pkg, '')}.{names[0]}")
        if not (sub and ntype.get(sub) == "module"):
            continue
        for h in holders(src, tgt):
            imap.setdefault(h, {}).setdefault(local, sub)

    def scope_chain(src: str, mod: str) -> list[str]:
        """src, its enclosing function scopes, then the module: imports
        inside a function body (`def t(): from x import y`) bind there."""
        out, q = [src], qname.get(src, "")
        while "." in q:
            q = q.rsplit(".", 1)[0]
            nid = by_q.get(q)
            if nid and nid != mod and ntype.get(nid) in ("function", "method", "test"):
                out.append(nid)
        out.append(mod)
        return out

    def imported(src: str, mod: str, local: str) -> str | None:
        for sid in scope_chain(src, mod):
            t = imap.get(sid, {}).get(local)
            if t:
                return t
        return None

    def member(container: str, name: str, depth: int = 0) -> str | None:
        """Symbol `name` defined in or re-exported by module/class `container`."""
        cq = qname.get(container, "")
        hit = by_q.get(f"{cq}.{name}")
        if hit:
            return hit
        if ntype.get(container) == "module" and depth < _CHAIN:
            t = imap.get(container, {}).get(name)
            if t:
                return t if ntype.get(t) != "module" else None
        return None

    # 4. class bases (spellings from python-ast inherits edges)
    bases_sp: dict[str, list[str]] = {}
    for src, tgt in cx.execute("SELECT source, target FROM edges WHERE relation='inherits' "
                               "AND method='python-ast'"):
        sp = tgt.split(":", 2)[-1] if tgt.startswith("ext:") else qname.get(tgt, "")
        sp = sp.split("[", 1)[0].strip()  # generic base: `Serializer[_T]`
        if sp:
            bases_sp.setdefault(src, []).append(sp)

    def module_node_for(nid: str) -> str | None:
        q = qname.get(nid, "")
        while q:
            m = by_q.get(q)
            if m and ntype.get(m) == "module":
                return m
            q = q.rsplit(".", 1)[0] if "." in q else ""
        return None

    def resolve_name(mod: str, spelling: str) -> str | None:
        parts = spelling.split(".")
        mq = qname.get(mod, "")
        if len(parts) == 1:
            return by_q.get(f"{mq}.{parts[0]}") or imap.get(mod, {}).get(parts[0])
        head = imap.get(mod, {}).get(parts[0])
        if head and ntype.get(head) == "module" and len(parts) == 2:
            return member(head, parts[1])
        return None

    def bases_of(cls_id: str) -> list[str]:
        mod = module_node_for(cls_id)
        out = []
        for sp in bases_sp.get(cls_id, []):
            b = resolve_name(mod, sp) if mod else None
            if b and qname.get(b) in classes:
                out.append(b)
        return out

    def mro(cls_id: str, include_self: bool) -> list[str]:
        out, seen, queue = [], set(), [(cls_id, include_self)]
        while queue and len(out) < _MRO:
            c, inc = queue.pop(0)
            if c in seen:
                continue
            seen.add(c)
            if inc:
                out.append(c)
            queue = [(b, True) for b in bases_of(c)] + queue
        return out

    def method_on(cls_list, m):
        for c in cls_list:
            hit = by_q.get(f"{qname[c]}.{m}")
            if hit and ntype.get(hit) in ("function", "method", "test"):
                return hit
        return None

    # 4b. receivers the call spelling cannot type by itself, read from the
    # source AST: `self.attr` (types assigned in the class) and pytest
    # fixtures (`def test_x(runner)` -> `runner()` fixture -> its return type)
    roots = list((repo_roots or {}).values())
    trees: dict = {}

    def tree_of(fp: str):
        if fp not in trees:
            trees[fp] = None
            for r in roots:
                p = Path(r) / fp
                if p.is_file():
                    try:
                        trees[fp] = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
                    except (SyntaxError, ValueError):
                        pass
                    break
        return trees[fp]

    def def_at(fp: str, line: int, kinds):
        t = tree_of(fp)
        if t is None:
            return None
        for n in ast.walk(t):
            if isinstance(n, kinds) and n.lineno == line:
                return n
        return None

    attr_cache: dict = {}

    def attr_types(cls_id: str) -> dict:
        """attr -> type spelling, from `self.x: T`, `self.x = p` (p: T) and `self.x = T(..)`."""
        if cls_id in attr_cache:
            return attr_cache[cls_id]
        out: dict = {}
        node = def_at(nfile.get(cls_id, ""), nline.get(cls_id, 0), ast.ClassDef)
        if node is not None:
            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    a = st.annotation
                    if isinstance(a, ast.Subscript) and (_dotted(a.value) or "").split(".")[-1] in ("type", "Type"):
                        sp = _ann(a.slice)               # `context_class: type[Context] = Context`
                        if sp:
                            out.setdefault(st.target.id, "type:" + sp)
                        continue
                    sp = _ann(a)
                    if sp:
                        out.setdefault(st.target.id, sp)
                elif isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) \
                        and isinstance(st.value, (ast.Name, ast.Attribute)):
                    sp = _dotted(st.value)               # `formatter_class = HelpFormatter`
                    if sp and sp.rsplit(".", 1)[-1][:1].isupper():
                        out.setdefault(st.targets[0].id, "type:" + sp)
            for fn in node.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                params = {a.arg: _ann(a.annotation) for a in fn.args.args + fn.args.kwonlyargs if a.annotation}
                for st in ast.walk(fn):
                    tgt, val, ann = None, None, None
                    if isinstance(st, ast.AnnAssign):
                        tgt, val, ann = st.target, st.value, st.annotation
                    elif isinstance(st, ast.Assign) and len(st.targets) == 1:
                        tgt, val = st.targets[0], st.value
                    if not (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"):
                        continue
                    sp = _ann(ann) if ann is not None else None
                    if not sp and isinstance(val, ast.Name) and params.get(val.id):
                        sp = params[val.id]
                    if not sp and isinstance(val, ast.Call):
                        sp = _dotted(val.func)
                    if sp:
                        out.setdefault(tgt.attr, sp)
            for fn in node.body:
                # `@property def headers(self) -> Headers`: read like an attribute
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.returns is not None and any(
                        (_dotted(dc) or "").rsplit(".", 1)[-1] in ("property", "cached_property")
                        for dc in fn.decorator_list):
                    sp = _ann(fn.returns)
                    if sp:
                        out.setdefault(fn.name, sp)
        attr_cache[cls_id] = out
        return out

    elem_cache: dict = {}

    def attr_elems(cls_id: str) -> dict:
        """attr -> element type spelling of a collection attribute
        (`routes: list[BaseRoute]`, `self.routes: Sequence[BaseRoute] = ...`,
        `self.middleware = list(middleware)` with `middleware: Sequence[M]`)."""
        if cls_id in elem_cache:
            return elem_cache[cls_id]
        out: dict = {}
        node = def_at(nfile.get(cls_id, ""), nline.get(cls_id, 0), ast.ClassDef)
        if node is not None:
            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    sp = _elem_ann(st.annotation)
                    if sp:
                        out.setdefault(st.target.id, sp)
            for fn in node.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.returns is not None and any((_dotted(dc) or "").rsplit(".", 1)[-1] in
                                                  ("property", "cached_property") for dc in fn.decorator_list):
                    sp = _elem_ann(fn.returns)
                    if sp:
                        out.setdefault(fn.name, sp)
                params = {a.arg: a.annotation for a in fn.args.args + fn.args.kwonlyargs if a.annotation}
                for st in ast.walk(fn):
                    tgt, val, ann = None, None, None
                    if isinstance(st, ast.AnnAssign):
                        tgt, val, ann = st.target, st.value, st.annotation
                    elif isinstance(st, ast.Assign) and len(st.targets) == 1:
                        tgt, val = st.targets[0], st.value
                    if not (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"):
                        continue
                    sp = _elem_ann(ann) if ann is not None else None
                    # `[] if routes is None else list(routes)` / `routes or []`
                    vals = [val]
                    if isinstance(val, ast.IfExp):
                        vals = [val.body, val.orelse]
                    elif isinstance(val, ast.BoolOp):
                        vals = list(val.values)
                    for v_ in vals:
                        if sp:
                            break
                        if isinstance(v_, ast.Call) and _dotted(v_.func) in ("list", "tuple", "sorted") \
                                and v_.args and isinstance(v_.args[0], ast.Name):
                            v_ = v_.args[0]
                        if isinstance(v_, ast.Name) and v_.id in params:
                            sp = _elem_ann(params[v_.id])
                    if sp:
                        out.setdefault(tgt.attr, sp)
        elem_cache[cls_id] = out
        return out

    mod_types_cache: dict = {}

    def module_global_type(mod: str, fp: str, name: str):
        """Class of a module-level `name = C(..)` / `name: C = ..` bound once."""
        key = (fp, name)
        if key in mod_types_cache:
            return mod_types_cache[key]
        res_ = None
        t = tree_of(fp)
        hits = []
        for st in (t.body if t is not None else []):
            if isinstance(st, ast.Assign) and any(isinstance(x, ast.Name) and x.id == name for x in st.targets):
                hits.append(st.value)
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.target.id == name:
                hits.append(st.annotation)
        if len(hits) == 1:
            v = hits[0]
            sp = _dotted(v.func) if isinstance(v, ast.Call) else _ann(v)
            c = resolve_name(mod, sp) if sp else None
            res_ = c if c and qname.get(c) in classes else None
        mod_types_cache[key] = res_
        return res_

    fixtures: dict = {}   # module id -> {fixture name: return type spelling}
    for fp, mid in mod_of_file.items():
        base = fp.rsplit("/", 1)[-1]
        if not (base == "conftest.py" or base.startswith("test_") or base.endswith("_test.py")):
            continue
        t = tree_of(fp)
        if t is None:
            continue
        for n in t.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    "fixture" in (_dotted(d.func if isinstance(d, ast.Call) else d) or "") for d in n.decorator_list):
                sp = _ann(n.returns) if n.returns is not None else None
                if not sp:
                    for st in ast.walk(n):
                        v = st.value if isinstance(st, (ast.Return, ast.Yield)) else None
                        if isinstance(v, ast.Call):
                            sp = _dotted(v.func)
                            break
                if sp:
                    fixtures.setdefault(mid, {})[n.name] = sp

    def fixture_type(src: str, name: str) -> str | None:
        """Class id of fixture `name` visible from test `src` (own module, then conftest.py upwards)."""
        fp = nfile.get(src, "")
        fn = def_at(fp, nline.get(src, 0), (ast.FunctionDef, ast.AsyncFunctionDef))
        if fn is None or name not in {a.arg for a in fn.args.args} or any(
                a.arg == name and a.annotation is not None for a in fn.args.args):
            return None
        d = fp.rsplit("/", 1)[0] if "/" in fp else ""
        cands = [fp]
        while True:
            cands.append(f"{d}/conftest.py" if d else "conftest.py")
            if not d:
                break
            d = d.rsplit("/", 1)[0] if "/" in d else ""
        for c in cands:
            mid = mod_of_file.get(c)
            sp = fixtures.get(mid, {}).get(name) if mid else None
            if sp:
                cls = resolve_name(mid, sp)
                return cls if cls and qname.get(cls) in classes else None
        return None

    ret_cache: dict = {}

    def returns_of(fn_id: str):
        """(class id, is_container) declared as `fn_id`'s return type."""
        if fn_id in ret_cache:
            return ret_cache[fn_id]
        ret_cache[fn_id] = None
        fn = def_at(nfile.get(fn_id, ""), nline.get(fn_id, 0), (ast.FunctionDef, ast.AsyncFunctionDef))
        if fn is not None and fn.returns is not None:
            r, many = fn.returns, False
            if isinstance(r, ast.BinOp) and isinstance(r.op, ast.BitOr):
                # `Callable[[F], Group] | Group`: either side names the product
                r = r.right if isinstance(r.left, ast.Subscript) and (_dotted(r.left.value) or "").endswith("Callable") \
                    and not isinstance(r.right, ast.Subscript) else r.left
            if isinstance(r, ast.Subscript) and (_dotted(r.value) or "").split(".")[-1] == "Callable" \
                    and isinstance(r.slice, ast.Tuple) and r.slice.elts:
                r = r.slice.elts[-1]       # a decorator factory: what it turns the function into
            if isinstance(r, ast.Subscript) and (_dotted(r.value) or "").split(".")[-1] in _CONTAINERS:
                r, many = (r.slice.elts[0] if isinstance(r.slice, ast.Tuple) and r.slice.elts else r.slice), True
            sp = _ann(r)
            m = module_node_for(fn_id)
            cls = resolve_name(m, sp) if sp and m else None
            if cls and qname.get(cls) in classes:
                ret_cache[fn_id] = (cls, many)
        return ret_cache[fn_id]

    local_cache: dict = {}
    except_ranges: dict = {}   # src -> [(name, first line, last line, class id)]

    def param_class(src: str, mod: str, name: str):
        """Class id of parameter `name` of `src` from its annotation."""
        fn = def_at(nfile.get(src, ""), nline.get(src, 0), (ast.FunctionDef, ast.AsyncFunctionDef))
        if fn is None:
            return None
        for a_ in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs:
            if a_.arg == name and a_.annotation is not None:
                sp = _ann(a_.annotation)
                c = resolve_name(mod, sp) if sp else None
                return c if c and qname.get(c) in classes else None
        return None

    bound_cache: dict = {}

    def locally_bound(src: str, name: str) -> bool:
        """`name` is bound in `src`'s own scope by an assignment, with/for/except
        target, import or an annotated parameter: its type is that binding's,
        never a naming convention's."""
        if src not in bound_cache:
            fn = def_at(nfile.get(src, ""), nline.get(src, 0), (ast.FunctionDef, ast.AsyncFunctionDef))
            root = fn if fn is not None else (tree_of(nfile.get(src, "")) if ntype.get(src) == "module" else None)
            names: set = set()
            if root is not None:
                if fn is not None:
                    names |= {a_.arg for a_ in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs
                              if a_.annotation is not None}
                todo = [root]
                while todo:
                    n = todo.pop()
                    if n is not root and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                        ast.ClassDef, ast.Lambda)):
                        names.add(getattr(n, "name", ""))
                        continue
                    if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                        names.add(n.id)
                    elif isinstance(n, ast.ExceptHandler) and n.name:
                        names.add(n.name)
                    elif isinstance(n, (ast.Import, ast.ImportFrom)):
                        names |= {(al.asname or al.name).split(".")[0] for al in n.names}
                    todo.extend(ast.iter_child_nodes(n))
            bound_cache[src] = names
        return name in bound_cache[src]

    _conv_box: list = []

    def py_conv() -> dict:
        """param name -> class id when the repo annotates that name with one
        repo class at least 5 times and in at least 90% of its annotations."""
        if _conv_box:
            return _conv_box[0]
        cnt: dict = {}
        for nid_, t_ in ntype.items():
            if t_ not in ("function", "method"):
                continue
            fp_ = nfile.get(nid_, "")
            mod_ = mod_of_file.get(fp_)
            fn = def_at(fp_, nline.get(nid_, 0), (ast.FunctionDef, ast.AsyncFunctionDef)) if mod_ else None
            if fn is None:
                continue
            for a_ in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs:
                if a_.annotation is None or a_.arg in ("self", "cls"):
                    continue
                sp = _ann(a_.annotation)
                c = resolve_name(mod_, sp) if sp else None
                key = c if c and qname.get(c) in classes else "?" + (sp or "")
                cnt.setdefault(a_.arg, collections.Counter())[key] += 1
        out = {}
        for nm_, c_ in cnt.items():
            tot = sum(c_.values())
            top, n_ = c_.most_common(1)[0]
            if tot >= 5 and n_ >= 0.9 * tot and not top.startswith("?"):
                out[nm_] = top
        _conv_box.append(out)
        return out

    def var_type(src: str, mod: str, name: str, line: int):
        """Class id of local `name` at `line` of `src` (except-handler first)."""
        local_types(src, mod)
        for nm, a, b, c in except_ranges.get(src, ()):
            if nm == name and a <= line <= b:
                return c
        lt = local_types(src, mod)
        if name in lt:
            return lt[name]
        if not locally_bound(src, name):
            # a module-level `app = Starlette(..)` used inside a function
            return module_global_type(mod, nfile.get(src, ""), name)
        return None

    def local_types(src: str, mod: str) -> dict:
        """name -> class id for locals of `src`: `v = C(..)`, `v = f(..)` / `self.m(..)`
        with a declared return type, `v: C = ..`, `for v in <returns list[C]>`."""
        if src in local_cache:
            return local_cache[src]
        out: dict = {}
        local_cache[src] = out
        fn = def_at(nfile.get(src, ""), nline.get(src, 0), (ast.FunctionDef, ast.AsyncFunctionDef))
        if fn is None:
            return out
        poisoned = set()

        def call_type(v):
            if not isinstance(v, ast.Call):
                return None
            sp = _dotted(v.func)
            if not sp:
                return None
            parts_ = sp.split(".")
            if len(parts_) == 2 and parts_[0] in ("self", "cls"):
                c = enclosing_class(src)
                f = method_on(mro(c, True), parts_[1]) if c else None
                return returns_of(f) if f else None
            if len(parts_) == 2:
                # `formatter = ctx.make_formatter()` with `ctx: Context` (or a
                # local typed above): the method's declared return type
                rc = param_class(src, mod, parts_[0]) or out.get(parts_[0])
                if rc is not None:
                    f = method_on(mro(rc, True), parts_[1])
                    if f is not None:
                        return returns_of(f)
                    # `generator = env.code_generator_class(..)`: a class (or
                    # callable) held in an attribute of the typed receiver
                    for k in mro(rc, True):
                        sp = attr_types(k).get(parts_[1])
                        if not sp:
                            continue
                        is_cls = sp.startswith("type:")
                        cls = resolve_name(module_node_for(k) or mod, sp[5:] if is_cls else sp)
                        if cls and qname.get(cls) in classes:
                            if is_cls:
                                return (cls, False)
                            f2 = method_on(mro(cls, True), "__call__")
                            return returns_of(f2) if f2 is not None else (cls, False)
                    return None
            if len(parts_) == 1:
                # `client = test_client_factory(app)` with `test_client_factory:
                # TestClientFactory` (a Protocol/class with __call__): calling a
                # typed object returns what its __call__ declares
                oc = param_class(src, mod, sp) or out.get(sp)
                if oc is not None:
                    f = method_on(mro(oc, True), "__call__")
                    return returns_of(f) if f is not None else None
            t = resolve_name(mod, sp) if len(parts_) <= 2 else None
            if t is None and len(parts_) == 1:
                t = imported(src, mod, sp)
            if t and qname.get(t) in classes:
                return (t, False)
            if t and ntype.get(t) in ("function", "method"):
                return returns_of(t)
            return None

        def own_scope(root):
            """ast.walk minus nested def/class/lambda bodies: their locals are
            theirs (a nested `websocket = WebSocket(..)` is not the outer
            `with client.websocket_connect() as websocket`). The nested def
            statement itself is still yielded (decorator rule below)."""
            todo = [root]
            while todo:
                n = todo.pop()
                yield n
                if n is not root and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                    ast.ClassDef, ast.Lambda)):
                    continue
                todo.extend(reversed(list(ast.iter_child_nodes(n))))

        for st in own_scope(fn):
            name, typ = None, None
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                sp = _ann(st.annotation)
                c = resolve_name(mod, sp) if sp else None
                name, typ = st.target.id, ((c, False) if c and qname.get(c) in classes else None)
            elif isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) \
                    and isinstance(st.value, ast.Subscript) and isinstance(st.value.value, ast.Name):
                # `convertor = param_convertors[key]` with `param_convertors: dict[str, Convertor]`
                name, typ = st.targets[0].id, None
                for a_ in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs:
                    if a_.arg == st.value.value.id and a_.annotation is not None:
                        vsp = _value_ann(a_.annotation)
                        cv = resolve_name(mod, vsp) if vsp else None
                        typ = (cv, False) if cv and qname.get(cv) in classes else None
            elif isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                name, typ = st.targets[0].id, call_type(st.value)
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                for it in st.items:
                    if isinstance(it.optional_vars, ast.Name):
                        r = call_type(it.context_expr)
                        if r and not r[1]:
                            prev = out.get(it.optional_vars.id)
                            if prev not in (None, r[0]):
                                poisoned.add(it.optional_vars.id)
                            out[it.optional_vars.id] = r[0]
                continue
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and st is not fn \
                    and st.decorator_list:
                # `@click.group() def cli()` / `@cli.command() def sub()`: the
                # name now holds what the outermost decorator returns
                r = call_type(st.decorator_list[0])
                if r and not r[1]:
                    if out.get(st.name) not in (None, r[0]):
                        poisoned.add(st.name)
                    out[st.name] = r[0]
                continue
            elif isinstance(st, ast.ExceptHandler) and st.name and st.type is not None:
                # `except ClickException as e:` types `e` inside this handler only
                sp = _dotted(st.type)
                c = resolve_name(mod, sp) if sp else None
                if c and qname.get(c) in classes:
                    except_ranges.setdefault(src, []).append(
                        (st.name, st.lineno, getattr(st, "end_lineno", st.lineno) or st.lineno, c))
                continue
            elif isinstance(st, (ast.For, ast.AsyncFor)) and isinstance(st.target, ast.Name):
                r = call_type(st.iter)
                name, typ = st.target.id, ((r[0], False) if r and r[1] else None)
                if typ is None:
                    # `for route in self.routes` (routes: list[BaseRoute]) /
                    # `for m in middleware` (middleware: Sequence[Middleware])
                    esp = None
                    it_ = st.iter
                    if isinstance(it_, ast.Attribute) and isinstance(it_.value, ast.Name) and it_.value.id == "self":
                        c_ = enclosing_class(src)
                        for k in (mro(c_, True) if c_ else []):
                            esp = attr_elems(k).get(it_.attr)
                            if esp:
                                mod_k = module_node_for(k) or mod
                                ce = resolve_name(mod_k, esp)
                                typ = (ce, False) if ce and qname.get(ce) in classes else None
                                break
                    elif isinstance(it_, ast.Name):
                        for a_ in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs:
                            if a_.arg == it_.id and a_.annotation is not None:
                                esp = _elem_ann(a_.annotation)
                                ce = resolve_name(mod, esp) if esp else None
                                typ = (ce, False) if ce and qname.get(ce) in classes else None
            else:
                continue
            if name is None:
                continue
            if typ is None or typ[1]:
                poisoned.add(name)       # reassigned to something we cannot type
                continue
            if out.get(name) not in (None, typ[0]):
                poisoned.add(name)
            out[name] = typ[0]
        for p_ in poisoned:
            out.pop(p_, None)
        return out

    def enclosing_class(src: str) -> str | None:
        q = qname.get(src, "")
        while "." in q:
            if q in classes:
                return by_q.get(q)
            q = q.rsplit(".", 1)[0]
        return None

    owners: dict[str, str] = {}
    for eid, fk in cx.execute("SELECT edge_id, file_key FROM file_edges"):
        owners.setdefault(eid, fk)
    owners_of_file: dict[str, str] = {}
    for fk, in cx.execute("SELECT DISTINCT file_key FROM file_nodes"):
        owners_of_file.setdefault(fk.split(":", 1)[-1], fk)

    # Distinctive method names defined once in the repo (see "unique-name"
    # below): long enough and with two word breaks, so `get_parameter_source`
    # qualifies and library-colliding names (`read`, `getvalue`, `update`) never do.
    _lab_count: dict = {}
    for nid_, t_ in ntype.items():
        if t_ in ("function", "method") and "." in qname.get(nid_, ""):
            _lab_count.setdefault(qname[nid_].rsplit(".", 1)[-1], []).append(nid_)
    unique_member: dict = {}
    for lab_, ids_ in _lab_count.items():
        if len(ids_) != 1 or lab_.startswith("__") or len(lab_) < 10                 or sum(1 for ch in lab_ if ch.isupper()) + lab_.count("_") < 2:
            continue
        owner_q = qname[ids_[0]].rsplit(".", 1)[0]
        if owner_q in classes:              # a method, not a module-level function
            unique_member[lab_] = ids_[0]
    # Property getters defined exactly once in the repo, keyed by property name.
    # Used to bind attribute reads on untyped receivers (`style.link` where
    # `style` has no known type) to the one getter that defines it.
    unique_property: dict = {}
    prop_counts: dict = {}
    for nid_, t_ in ntype.items():
        if t_ not in ("function", "method") or "." not in qname.get(nid_, ""):
            continue
        fp_ = nfile.get(nid_)
        ln_ = nline.get(nid_, 0)
        node_ = def_at(fp_, ln_, (ast.FunctionDef, ast.AsyncFunctionDef))
        if node_ is None:
            continue
        is_prop_ = any((_dotted(dc) or "").rsplit(".", 1)[-1] in ("property", "cached_property")
                      for dc in getattr(node_, "decorator_list", []))
        if not is_prop_:
            continue
        lab_ = qname[nid_].rsplit(".", 1)[-1]
        prop_counts.setdefault(lab_, []).append(nid_)
    for lab_, ids_ in prop_counts.items():
        if len(ids_) == 1:
            unique_property[lab_] = ids_[0]
    unique_attr_blockers = {"self", "cls"}

    # 5. bind python unresolved calls
    new_edges: list[tuple[Edge, str]] = []
    bound_by_id: dict = {}
    stats = {"py_unresolved": 0, "bound": 0}
    for row in cx.execute("SELECT * FROM edges WHERE relation='calls_unresolved' "
                          "AND method='python-call-resolution'").fetchall():
        stats["py_unresolved"] += 1
        d = _j(row["attributes"])
        raw = d.get("unresolved_call") or ""
        src = row["source"]
        mod = mod_of_file.get(row["file_path"] or "")
        if not raw or not mod or src not in ntype:
            continue
        parts = raw.split(".")
        tgt, via = None, ""
        if len(parts) == 1:
            t = imported(src, mod, raw)
            if t and ntype.get(t) in ("function", "class"):
                tgt, via = t, "import"
        elif len(parts) == 2:
            base, m = parts
            if base in ("self", "cls", "super"):
                c = enclosing_class(src)
                if c:
                    tgt = method_on(mro(c, base != "super"), m)
                    via = "heritage"
                    if tgt is None and base != "super":
                        # `self.context_class(..)` (a class held in an attribute)
                        # or `self.type(value)` (a callable instance: __call__)
                        for k in mro(c, True):
                            sp = attr_types(k).get(m)
                            if not sp:
                                continue
                            is_cls = sp.startswith("type:")
                            cls = resolve_name(module_node_for(k) or mod, sp[5:] if is_cls else sp)
                            if cls and qname.get(cls) in classes:
                                tgt = cls if is_cls else method_on(mro(cls, True), "__call__")
                                via = "class-attribute" if is_cls else "callable-attribute"
                            break
            elif d.get("receiver_type"):
                # `def f(ctx: Context)` then `ctx.m()`: declared parameter
                # type (syntax), resolved through this module's imports.
                c = resolve_name(mod, d["receiver_type"])
                if c and qname.get(c) in classes:
                    tgt = method_on(mro(c, True), m)
                    via = "annotation"
                    if tgt is None:
                        # `ctx.code_generator_class(..)`: a class (or callable)
                        # held in an attribute of the declared type
                        for k in mro(c, True):
                            sp = attr_types(k).get(m)
                            if not sp:
                                continue
                            is_cls = sp.startswith("type:")
                            cls = resolve_name(module_node_for(k) or mod, sp[5:] if is_cls else sp)
                            if cls and qname.get(cls) in classes:
                                tgt = cls if is_cls else method_on(mro(cls, True), "__call__")
                                via = "class-attribute" if is_cls else "callable-attribute"
                                break
            else:
                h = imported(src, mod, base)
                if h and ntype.get(h) == "module":
                    t = member(h, m)
                    if t and ntype.get(t) in ("function", "class"):
                        tgt, via = t, "module"
                elif h and qname.get(h) in classes:
                    tgt, via = method_on(mro(h, True), m), "imported-class"
                elif h is None:
                    c = fixture_type(src, base)
                    if c:
                        tgt, via = method_on(mro(c, True), m), "pytest-fixture"
                    else:
                        c = var_type(src, mod, base, row["start_line"] or 0)
                        if c:
                            tgt, via = method_on(mro(c, True), m), "local-type"
        elif len(parts) == 3 and parts[0] == "self":
            # `self.command.get_help()`: the attribute's type assigned in the class
            c = enclosing_class(src)
            for k in (mro(c, True) if c else []):
                sp = attr_types(k).get(parts[1])
                if sp:
                    cls = resolve_name(module_node_for(k) or mod, sp)
                    if cls and qname.get(cls) in classes:
                        tgt, via = method_on(mro(cls, True), parts[2]), "self-attribute"
                    break
        elif len(parts) == 3 and parts[0] not in ("cls", "super"):
            # `ctx.command.get_params()`: a typed parameter/local, then the
            # attribute's type declared in that class
            c0 = param_class(src, mod, parts[0]) or var_type(src, mod, parts[0], row["start_line"] or 0)
            for k in (mro(c0, True) if c0 else []):
                sp = attr_types(k).get(parts[1])
                if sp:
                    cls = resolve_name(module_node_for(k) or mod, sp[5:] if sp.startswith("type:") else sp)
                    if cls and qname.get(cls) in classes and not sp.startswith("type:"):
                        tgt, via = method_on(mro(cls, True), parts[2]), "attribute-chain"
                    break
        cclass_, score_ = "resolved", 0.85
        if not tgt and len(parts) >= 2 and parts[0] not in ("self", "cls", "super") \
                and imported(src, mod, parts[0]) is None and not locally_bound(src, parts[0]):
            # Naming convention: the repo annotates `request: Request` (nearly)
            # everywhere, so an unannotated `request` parameter (a callback, a
            # dependency) is a Request too -- if the method exists on it.
            c0 = py_conv().get(parts[0])
            for seg in parts[1:-1]:
                if c0 is None:
                    break
                nxt = None
                for k in mro(c0, True):
                    sp = attr_types(k).get(seg)
                    if sp and not sp.startswith("type:"):
                        cls = resolve_name(module_node_for(k) or mod, sp)
                        nxt = cls if cls and qname.get(cls) in classes else None
                        break
                c0 = nxt
            t_ = method_on(mro(c0, True), parts[-1]) if c0 else None
            if t_:
                tgt, via, cclass_, score_ = t_, "name-convention", "hypothetical", 0.6
        if not tgt and len(parts) >= 2 and parts[0] not in ("self", "cls", "super"):
            # Last resort for an untyped receiver (`def cli(ctx): ctx.get_parameter_source()`):
            # a distinctive method name defined exactly once in the repo, on a
            # receiver that is not a typed repo object nor an imported module.
            m_ = parts[-1]
            cands_ = unique_member.get(m_)
            if cands_ and len(parts) == 2 and imported(src, mod, parts[0]) is None \
                    and not (param_class(src, mod, parts[0]) or var_type(src, mod, parts[0], row["start_line"] or 0)):
                tgt, via, cclass_, score_ = cands_, "unique-name", "hypothetical", 0.6
            elif cands_ and len(parts) == 3 and parts[1] not in unique_attr_blockers:
                tgt, via, cclass_, score_ = cands_, "unique-name", "hypothetical", 0.6
        if not tgt and len(parts) == 2 and parts[0] not in ("self", "cls", "super") \
                and _is_protocol(parts[1]) and imported(src, mod, parts[0]) is None:
            # `renderable.__rich_console__(..)`: a repo-defined protocol called on
            # an untyped object reaches every class that implements it
            impl = [i for i in _lab_count.get(parts[1], ()) if i != src][:200]
            for t_ in impl:
                e = Edge(canonical_edge_id(src, "dispatches", t_, row["file_path"] or "", "protocol:" + parts[1]),
                         src, t_, "dispatches", "inferred", "hypothetical", 0.55, [], METHOD, VERSION,
                         file_path=row["file_path"], start_line=row["start_line"],
                         attributes={"via": "protocol-method", "call_spelling": raw[:200]})
                if e.id not in bound_by_id:
                    bound_by_id[e.id] = e
                    new_edges.append((e, owners.get(row["id"], "")))
            if impl:
                cx.execute("INSERT OR REPLACE INTO superseded_edges VALUES(?,?)",
                           (row["id"], canonical_edge_id(src, "dispatches", impl[0], row["file_path"] or "",
                                                         "protocol:" + parts[1])))
                stats["bound"] += 1
            continue
        if not tgt or tgt == src and base_is_super(parts):
            continue
        site = site_fingerprint(raw[:200], row["file_path"] or "")
        attrs = {"call_spelling": raw[:200], "via": via}
        if d.get("occurrences"):
            attrs["occurrences"] = d["occurrences"]
        e = Edge(canonical_edge_id(src, "calls", tgt, row["file_path"] or "", site),
                 src, tgt, "calls", "inferred" if cclass_ == "hypothetical" else "resolved", cclass_, score_,
                 json.loads(row["provenance"] or "[]"), METHOD, VERSION,
                 file_path=row["file_path"], start_line=row["start_line"],
                 attributes=attrs)
        prev = bound_by_id.get(e.id)
        if prev is not None:
            # same caller, callee and spelling from several call rows (`f(a)` and
            # `f(a, k=1)` are split by shape): one edge holding every site, not
            # the last row's sites only
            occ = list(prev.attributes.get("occurrences") or [{"line": prev.start_line}])
            seen_l = {o.get("line") for o in occ}
            for o in (attrs.get("occurrences") or [{"line": row["start_line"]}]):
                if o.get("line") not in seen_l:
                    occ.append(o)
                    seen_l.add(o.get("line"))
            prev.attributes = dict(prev.attributes, occurrences=sorted(occ, key=lambda o: o.get("line") or 0))
            prev.start_line = min(prev.start_line or 0, row["start_line"] or 0) or prev.start_line
        else:
            bound_by_id[e.id] = e
            new_edges.append((e, owners.get(row["id"], "")))
        cx.execute("INSERT OR REPLACE INTO superseded_edges VALUES(?,?)", (row["id"], e.id))
        stats["bound"] += 1
    # 5a. callbacks: a repo function passed by name as an argument
    # (`Option(callback=show_help)`, `Thread(target=work)`, `app.add(view)`)
    # is invoked later through an attribute; record `references` so its
    # dependents (and tests) are found
    def_ids: dict = {}
    for nid, fp in nfile.items():
        if ntype.get(nid) in ("function", "method", "test"):
            def_ids[(fp, nline.get(nid, 0))] = nid
    seen_ref = set()

    def cm_class(own: str, mid: str, x):
        """Class of the object a `with` item enters, when the graph can tell."""
        if isinstance(x, ast.Name):
            return var_type(own, mid, x.id, x.lineno) or param_class(own, mid, x.id)
        if not isinstance(x, ast.Call):
            return None
        f = x.func
        t_ = None
        if isinstance(f, ast.Name):
            t_ = imported(own, mid, f.id) or by_q.get(f"{qname.get(mid, '')}.{f.id}")
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            b = f.value.id
            if b in ("self", "cls"):
                c0 = enclosing_class(own)
            else:
                h = imported(own, mid, b)
                if h and ntype.get(h) == "module":
                    t_ = member(h, f.attr)
                    c0 = None
                else:
                    c0 = h if h and qname.get(h) in classes else (
                        var_type(own, mid, b, x.lineno) or param_class(own, mid, b) or fixture_type(own, b))
            if t_ is None and c0:
                t_ = method_on(mro(c0, True), f.attr)
        if not t_:
            return None
        if qname.get(t_) in classes:
            return t_
        r = returns_of(t_) if ntype.get(t_) in ("function", "method") else None
        return r[0] if r and not r[1] else None

    subclasses: dict = {}
    methods_of: dict = {}      # class qualified name -> [(method name, id)]

    def prefixed_methods(c: str, pre: str) -> list:
        if not methods_of:
            for nid, t in ntype.items():
                if t in ("function", "method") and "." in qname.get(nid, ""):
                    owner_, name_ = qname[nid].rsplit(".", 1)
                    if owner_ in classes:
                        methods_of.setdefault(owner_, []).append((name_, nid))
            for q in classes:
                k = by_q.get(q)
                for b in (bases_of(k) if k else ()):
                    subclasses.setdefault(b, []).append(k)
        fam, todo = set(mro(c, True)), [c]
        while todo and len(fam) < 64:
            for s in subclasses.get(todo.pop(), ()):
                if s not in fam:
                    fam.add(s)
                    todo.append(s)
        out = [nid for k in fam for name_, nid in methods_of.get(qname.get(k, ""), ()) if name_.startswith(pre)]
        return out[:300]

    for fp, mid in mod_of_file.items():
        t = tree_of(fp)
        if t is None:
            continue
        stack = [(t, None)]
        called: set = set()
        for n_ in ast.walk(t):
            if isinstance(n_, ast.Call):
                called.add(id(n_.func))
        while stack:
            node, owner = stack.pop()
            for ch in ast.iter_child_nodes(node):
                own = owner
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    own = def_ids.get((fp, ch.lineno), owner)
                if isinstance(ch, ast.Attribute) and own and isinstance(ch.ctx, ast.Load) \
                        and isinstance(ch.value, ast.Name) and id(ch) not in called \
                        and ch.value.id not in ("self", "cls"):
                    # `report = e.show` / `cb = runner.invoke`: a bound method taken
                    # as a value is called later
                    c = var_type(own, mid, ch.value.id, ch.lineno)
                    t_ = method_on(mro(c, True), ch.attr) if c else None
                    if t_ is None and ch.value.id not in ("self", "cls"):
                        t_ = unique_property.get(ch.attr)
                    if t_ and t_ != own and (own, t_) not in seen_ref:
                        seen_ref.add((own, t_))
                        e = Edge(canonical_edge_id(own, "references", t_, fp, "bound-method"),
                                 own, t_, "references", "resolved", "resolved", 0.75, [], METHOD, VERSION,
                                 file_path=fp, start_line=ch.lineno, attributes={"via": "bound-method"})
                        new_edges.append((e, owners_of_file.get(fp, "")))
                if isinstance(ch, ast.Assign) and own and isinstance(ch.value, ast.Attribute) \
                        and isinstance(ch.value.value, ast.Name) and ch.value.value.id == "self":
                    # `render = self.render` then `render(..)`: a method kept in a local
                    c = enclosing_class(own)
                    t_ = method_on(mro(c, True), ch.value.attr) if c else None
                    if t_ and t_ != own and (own, t_) not in seen_ref:
                        seen_ref.add((own, t_))
                        e = Edge(canonical_edge_id(own, "references", t_, fp, "bound-method"),
                                 own, t_, "references", "resolved", "resolved", 0.75, [], METHOD, VERSION,
                                 file_path=fp, start_line=ch.lineno, attributes={"via": "bound-method"})
                        new_edges.append((e, owners_of_file.get(fp, "")))
                if isinstance(ch, (ast.With, ast.AsyncWith)) and own:
                    # `with X(..)` / `with obj.make()` / `with lock:` runs the
                    # context manager's __enter__/__exit__ without a visible call
                    names_ = ("__aenter__", "__aexit__") if isinstance(ch, ast.AsyncWith) else ("__enter__", "__exit__")
                    for item in ch.items:
                        c = cm_class(own, mid, item.context_expr)
                        for mname in (names_ if c else ()):
                            t_ = method_on(mro(c, True), mname)
                            if t_ and t_ != own and (own, t_) not in seen_ref:
                                seen_ref.add((own, t_))
                                e = Edge(canonical_edge_id(own, "calls", t_, fp, "with:" + mname),
                                         own, t_, "calls", "resolved", "resolved", 0.8, [], METHOD, VERSION,
                                         file_path=fp, start_line=ch.lineno, attributes={"via": "with-statement"})
                                new_edges.append((e, owners_of_file.get(fp, "")))
                if isinstance(ch, ast.Call) and own and isinstance(ch.func, ast.Name) and ch.func.id == "getattr" \
                        and len(ch.args) >= 2 and isinstance(ch.args[0], ast.Name) and ch.args[0].id == "self":
                    # `getattr(self, "visit_" + kind)`: every `visit_*` method of the
                    # class, its bases and its subclasses may be the one called
                    pre = _str_prefix(ch.args[1])
                    c = enclosing_class(own) if pre and len(pre) >= 3 else None
                    for t_ in (prefixed_methods(c, pre) if c else ()):
                        if t_ != own and (own, t_) not in seen_ref:
                            seen_ref.add((own, t_))
                            e = Edge(canonical_edge_id(own, "dispatches", t_, fp, "getattr:" + pre),
                                     own, t_, "dispatches", "inferred", "hypothetical", 0.6, [], METHOD, VERSION,
                                     file_path=fp, start_line=ch.lineno,
                                     attributes={"via": "getattr-prefix", "prefix": pre})
                            new_edges.append((e, owners_of_file.get(fp, "")))
                if isinstance(ch, ast.Call):
                    called.add(id(ch.func))
                if isinstance(ch, ast.Call) and own:
                    for v in list(ch.args) + [k.value for k in ch.keywords]:
                        tgt = None
                        if isinstance(v, ast.Name):
                            # a def nested in the caller (`key=sort_items`), then imports, then the module
                            tgt = by_q.get(f"{qname.get(own, '')}.{v.id}") or imported(own, mid, v.id) \
                                or by_q.get(f"{qname.get(mid, '')}.{v.id}")
                        elif isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == "self":
                            c = enclosing_class(own)
                            tgt = method_on(mro(c, True), v.attr) if c else None
                        if not tgt or tgt == own or ntype.get(tgt) not in ("function", "method") \
                                or (own, tgt) in seen_ref:
                            continue
                        seen_ref.add((own, tgt))
                        e = Edge(canonical_edge_id(own, "references", tgt, fp, "callback"),
                                 own, tgt, "references", "resolved", "resolved", 0.75, [], METHOD, VERSION,
                                 file_path=fp, start_line=v.lineno, attributes={"via": "callback-arg"})
                        new_edges.append((e, owners_of_file.get(fp, "")))
                stack.append((ch, own))
    store.upsert_edges([e for e, _ in new_edges])
    for e, fk in new_edges:
        if fk:
            cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (fk, e.id))

    # 5b. receiver-typed member calls in the other languages
    from .typed_callbind import bind_typed_receivers
    for k, v in bind_typed_receivers(store).items():
        stats["typed_" + k] = v

    # 6. supersede any-language unresolved rows covered by a resolved edge
    label = {r[0]: r[1] for r in cx.execute("SELECT id, label FROM nodes")}
    resolved_at: dict[tuple, dict[str, str]] = {}
    for eid, s, t, ln in cx.execute("SELECT id, source, target, start_line FROM edges "
                                    "WHERE relation IN ('calls','instantiates')"):
        resolved_at.setdefault((s, ln), {}).setdefault(label.get(t, ""), eid)
    sup = 0
    for eid, s, t, ln, a in cx.execute(
            "SELECT id, source, target, start_line, attributes FROM edges "
            "WHERE relation='calls_unresolved' "
            # Python sites are recorded exactly in step 5 (the extractor
            # emits one edge per site); a same-line label match there could
            # hide a different call (`self.update(x.update())`).
            "AND method != 'python-call-resolution'").fetchall():
        d = _j(a)
        spell = (d.get("unresolved_call") or t.split(":", 2)[-1]).replace("import:", "")
        name = d.get("member") or spell.rsplit(".", 1)[-1]
        by = resolved_at.get((s, ln), {}).get(name)
        if by:
            cx.execute("INSERT OR IGNORE INTO superseded_edges VALUES(?,?)", (eid, by))
            sup += 1
    stats["superseded"] = sup
    return stats


_CONTAINERS = {"list", "List", "Sequence", "Iterable", "Iterator", "Generator", "tuple", "Tuple", "set", "Set",
               "frozenset", "Collection", "MutableSequence", "AbstractSet", "KeysView", "ValuesView"}


# Python's own dunders: calls to these on an untyped object say nothing about
# which repo class is meant (every class has __init__/__eq__/...)
_BUILTIN_DUNDERS = {
    "__init__", "__new__", "__del__", "__repr__", "__str__", "__bytes__", "__format__", "__hash__", "__bool__",
    "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__getattr__", "__getattribute__", "__setattr__",
    "__delattr__", "__dir__", "__get__", "__set__", "__delete__", "__set_name__", "__init_subclass__",
    "__class_getitem__", "__call__", "__len__", "__length_hint__", "__getitem__", "__setitem__", "__delitem__",
    "__missing__", "__iter__", "__next__", "__reversed__", "__contains__", "__enter__", "__exit__", "__aenter__",
    "__aexit__", "__await__", "__aiter__", "__anext__", "__copy__", "__deepcopy__", "__reduce__",
    "__reduce_ex__", "__getstate__", "__setstate__", "__getnewargs__", "__getnewargs_ex__", "__sizeof__",
    "__fspath__", "__index__", "__int__", "__float__", "__complex__", "__round__", "__trunc__", "__floor__",
    "__ceil__", "__instancecheck__", "__subclasscheck__", "__subclasshook__", "__post_init__", "__html__",
}


def _is_protocol(name: str) -> bool:
    return (len(name) > 4 and name.startswith("__") and name.endswith("__") and name not in _BUILTIN_DUNDERS
            and not any(name == f"__{p}{o}__" for p in ("", "r", "i")
                        for o in ("add", "sub", "mul", "matmul", "truediv", "floordiv", "mod", "divmod", "pow",
                                  "lshift", "rshift", "and", "xor", "or", "neg", "pos", "abs", "invert")))


def _str_prefix(n) -> str | None:
    """`"visit_" + x` / `f"visit_{x}"` / `"visit_%s" % x` -> "visit_"."""
    if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Mod)):
        n = n.left
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return n.value.split("%", 1)[0] if "%" in n.value else n.value
        return _str_prefix(n) if isinstance(n, ast.BinOp) else None
    if isinstance(n, ast.JoinedStr) and n.values and isinstance(n.values[0], ast.Constant):
        return str(n.values[0].value)
    return None


def _dotted(n) -> str | None:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        b = _dotted(n.value)
        return f"{b}.{n.attr}" if b else None
    return None


def _ann(n) -> str | None:
    """`C`, `mod.C`, `Optional[C]`, `C | None`, `"C"` -> the class spelling."""
    if n is None:
        return None
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        try:
            return _ann(ast.parse(n.value, mode="eval").body)
        except SyntaxError:
            return None
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
        sides = [x for x in (n.left, n.right) if not (isinstance(x, ast.Constant) and x.value is None)]
        return _ann(sides[0]) if len(sides) == 1 else None
    if isinstance(n, ast.Subscript):
        _base = (_dotted(n.value) or "").rsplit(".", 1)[-1]
        if _base in ("type", "Type"):
            _inner = _ann(n.slice)
            if _inner:
                return "type:" + _inner
        if _base == "Optional":
            return _ann(n.slice)
    d = _dotted(n)
    return d if d and d[:1].isupper() or (d and "." in d and d.rsplit(".", 1)[-1][:1].isupper()) else None


_COLLECTIONS = {"list", "List", "Sequence", "MutableSequence", "Iterable", "Iterator", "Collection",
                "set", "Set", "frozenset", "FrozenSet", "AbstractSet", "MutableSet", "tuple", "Tuple", "deque"}


def _elem_ann(n) -> str | None:
    """Element class of a collection annotation: `list[C]`, `Sequence[C] | None`,
    `tuple[C, ...]` -> C."""
    if n is None:
        return None
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        try:
            return _elem_ann(ast.parse(n.value, mode="eval").body)
        except SyntaxError:
            return None
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
        sides = [x for x in (n.left, n.right) if not (isinstance(x, ast.Constant) and x.value is None)]
        return _elem_ann(sides[0]) if len(sides) == 1 else None
    if isinstance(n, ast.Subscript):
        base = (_dotted(n.value) or "").rsplit(".", 1)[-1]
        if base == "Optional":
            return _elem_ann(n.slice)
        if base in _COLLECTIONS:
            sl = n.slice
            if isinstance(sl, ast.Tuple):
                elts = sl.elts
                if len(elts) == 2 and isinstance(elts[1], ast.Constant) and elts[1].value is Ellipsis:
                    sl = elts[0]
                else:
                    return None
            return _ann(sl)
    return None


_MAPPINGS = {"dict", "Dict", "Mapping", "MutableMapping", "OrderedDict", "defaultdict", "ChainMap"}


def _value_ann(n) -> str | None:
    """Value class of a subscriptable annotation: `dict[str, C]` -> C,
    `list[C]` / `Sequence[C]` -> C."""
    if n is None:
        return None
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
        sides = [x for x in (n.left, n.right) if not (isinstance(x, ast.Constant) and x.value is None)]
        return _value_ann(sides[0]) if len(sides) == 1 else None
    if isinstance(n, ast.Subscript):
        base = (_dotted(n.value) or "").rsplit(".", 1)[-1]
        if base in _MAPPINGS and isinstance(n.slice, ast.Tuple) and len(n.slice.elts) == 2:
            v = n.slice.elts[1]
            if isinstance(v, ast.Subscript):
                v = v.value                          # `Convertor[Any]` -> Convertor
            return _ann(v)
        return _elem_ann(n)
    return None


def base_is_super(parts) -> bool:
    return bool(parts) and parts[0] == "super"
