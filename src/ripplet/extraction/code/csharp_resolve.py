"""C# namespace-scoped resolution (Phase 40 Wave 2).

Binds ``ts-c_sharp`` placeholders: namespace declarations to files
(C# compilation is namespace-based; directories are irrelevant),
plain/static/alias usings (outward search: current namespace outward
then global — real compiler order, first hit wins), same-namespace and
same-class calls, `new X()` instantiations, and heritage. `System.*`
(and friends) are external-stdlib. Object-variable calls stay
unresolved (no flow analysis).

Conventions mirror ``go_resolve``: repo-scoped inputs, manifest-owned
outputs, method ``csharp-resolve`` v1, deterministic sites, single
targets only (split namespaces with duplicate labels stay ambiguous).
"""
from __future__ import annotations

METHOD = "csharp-resolve"
VERSION = "4"  # v4: local functions shadow class members; v3: types found through the file's plain usings (new X, X.M()); v2: bare calls through `using static` types

_STDLIB_ROOTS = frozenset({"System", "Microsoft", "Windows"})


def resolve_repo(repo_id: str, repo_root: str, file_set: set[str],
                 new_results: list, store, exclude_keys: set[str] | None = None,
                 fresh_keys: set[str] | None = None, prof=None) \
        -> tuple[list, list, dict]:
    exclude_keys = exclude_keys or set()
    fresh_rels = {k.split(":", 1)[-1] for k in (fresh_keys or set())}
    if prof is None:
        from ..evaluation.stageprofile import make_profiler as _mp
        prof = _mp(False)
    prof.begin("csharp_collect")
    from ...core.models import Edge as EdgeCls
    from ...core.identifiers import file_node_id, canonical_edge_id
    from ...core.identifiers import site_fingerprint as _sf
    diag: dict = {"resolved_imports": 0, "external_imports": 0,
                  "unresolved_imports": 0, "depends_on_derived": 0,
                  "resolved_calls": 0, "ambiguous_calls": 0,
                  "resolved_heritage": 0}

    def owner_rel(key: str) -> str:
        return key.split(":", 1)[-1]

    def mod_id(rel: str) -> str:
        return file_node_id(repo_id, rel + "#module")

    def _attrs(d: dict) -> dict:
        import json as _json
        a = d.get("attributes") or {}
        if isinstance(a, str):
            try:
                a = _json.loads(a)
            except Exception:
                return {}
        return a if isinstance(a, dict) else {}

    # ---- collect ts-c_sharp edges
    imports: list[tuple[str, dict]] = []
    unres_calls: list[tuple[str, dict]] = []
    heritage: list[tuple[str, dict]] = []
    instantiates: list[tuple[str, dict]] = []
    repo_keys = sorted(k for k in store.list_file_keys()
                       if k.startswith(repo_id + ":") and k not in exclude_keys
                       and k.split(":", 1)[-1] not in fresh_rels)
    for key in repo_keys:
        for eid in sorted(store.get_manifest_edges(key)):
            row = store.cx.execute("SELECT * FROM edges WHERE id=?",
                                   (eid,)).fetchone()
            if row is None:
                continue
            d = dict(row)
            if not (d["method"] or "").startswith("ts-c_sharp"):
                continue
            if d["relation"] == "imports":
                imports.append((key, d))
            elif d["relation"] == "calls_unresolved":
                unres_calls.append((key, d))
            elif d["relation"] in ("inherits", "implements"):
                heritage.append((key, d))
            elif d["relation"] == "instantiates":
                instantiates.append((key, d))
    for key, res in new_results:
        for e in res.edges:
            if not (e.extraction_method or "").startswith("ts-c_sharp"):
                continue
            d = {"id": e.id, "source": e.source, "target": e.target,
                 "relation": e.relation,
                 "method": e.extraction_method,
                 "provenance": list(e.provenance_ids or []),
                 "attributes": dict(e.attributes or {}),
                 "file_path": e.file_path, "start_line": e.start_line}
            if e.relation == "imports":
                imports.append((key, d))
            elif e.relation == "calls_unresolved":
                unres_calls.append((key, d))
            elif e.relation in ("inherits", "implements"):
                heritage.append((key, d))
            elif e.relation == "instantiates":
                instantiates.append((key, d))

    # ---- node lookup + per-file symbol tables
    # Memoized (perf, identical results): resolver loops looked nodes up per
    # call with one store query each, plus a linear scan of new_results.
    _new_nodes: dict = {}
    for _k, _res in new_results:
        for _m in getattr(_res, "nodes", []):
            _new_nodes.setdefault(getattr(_m, "id", ""), _m)
    _node_cache: dict = {}

    def _node(nid: str):
        if nid in _node_cache:
            return _node_cache[nid]
        n = store.get_node(nid)
        if n is None:
            n = _new_nodes.get(nid)
        _node_cache[nid] = n
        return n

    file_syms: dict[str, dict[str, list[str]]] = {}
    # rel -> label -> [node ids] (methods share plain labels)
    file_ns: dict[str, str] = {}
    # rel -> enclosing namespace ("" = global)
    ns_types: dict[str, dict[str, list[str]]] = {}
    # dotted ns -> type label -> [node ids] (top-level types only)

    def _add_sym(rel: str, node):
        if getattr(node, "type", "") not in ("function", "method", "class",
                                             "interface", "type"):
            return
        file_syms.setdefault(rel, {}).setdefault(
            getattr(node, "label", ""), []).append(node.id)

    for key in repo_keys:
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is not None and (n.file_path or "") == owner_rel(key):
                _add_sym(owner_rel(key), n)
    for key, res in new_results:
        for n in getattr(res, "nodes", []):
            if (getattr(n, "file_path", "") or "") == owner_rel(key):
                _add_sym(owner_rel(key), n)

    def _add_sym(rel: str, node):
        if getattr(node, "type", "") not in ("function", "method", "class",
                                             "interface", "type"):
            return
        file_syms.setdefault(rel, {}).setdefault(
            getattr(node, "label", ""), []).append(node.id)

    def _qnames(node) -> str:
        return getattr(node, "qualified_name", "") or ""

    # namespace index from top-level types (defines-parent is the module)
    for key in repo_keys:
        for eid in store.get_manifest_edges(key):
            row = store.cx.execute("SELECT * FROM edges WHERE id=?",
                                   (eid,)).fetchone()
            if row is None:
                continue
            d = dict(row)
            if d["relation"] != "defines":
                continue
            s = _node(d["source"])
            t = _node(d["target"])
            if s is None or t is None:
                continue
            if getattr(s, "type", "") == "module" and \
                    getattr(t, "type", "") in ("class", "interface", "type"):
                qn = _qnames(t)
                if "." in qn:
                    ns, _, _label = qn.rpartition(".")
                    ns_types.setdefault(ns, {}).setdefault(
                        getattr(t, "label", ""), []).append(t.id)
                    if (t.file_path or "") and \
                            (t.file_path or "") not in file_ns:
                        file_ns[t.file_path or ""] = ns
    for key, res in new_results:
        for e in res.edges:
            if e.relation != "defines":
                continue
            s = _node(e.source)
            t = _node(e.target)
            if s is None or t is None:
                continue
            if getattr(s, "type", "") == "module" and \
                    getattr(t, "type", "") in ("class", "interface", "type"):
                qn = _qnames(t)
                if "." in qn:
                    ns, _, _label = qn.rpartition(".")
                    ns_types.setdefault(ns, {}).setdefault(
                        getattr(t, "label", ""), []).append(t.id)
                    if (t.file_path or "") and \
                            (t.file_path or "") not in file_ns:
                        file_ns[t.file_path or ""] = ns

    def _owner_class(node_id: str) -> str:
        n = _node(node_id)
        if n is None or getattr(n, "type", "") != "method":
            return ""
        parts = (getattr(n, "qualified_name", "") or "").split(".")
        return ".".join(parts[:-1]) if len(parts) >= 2 else ""

    _methods_memo: dict = {}

    def _methods_of(owner_qn: str, member: str) -> list[str]:
        _mk = (owner_qn, member)
        if _mk in _methods_memo:
            return list(_methods_memo[_mk])
        out = []
        for rel, tbl in file_syms.items():
            for nid in tbl.get(member, []) or []:
                n = _node(nid)
                if n is not None and getattr(n, "type", "") == "method" \
                        and _owner_class(nid) == owner_qn:
                    out.append(nid)
        _methods_memo[_mk] = sorted(set(out))
        return list(_methods_memo[_mk])

    def _outward_ns(here_ns: str) -> list[str]:
        parts = here_ns.split(".") if here_ns else []
        return [".".join(parts[:i]) for i in range(len(parts), -1, -1)]

    def _find_type(name: str, here_ns: str, extra_ns: list[str] | None = None):
        """Outward + using namespaces, single target or None."""
        seen: list[str] = []
        for ns in _outward_ns(here_ns) + list(extra_ns or []):
            for nid in (ns_types.get(ns, {}) or {}).get(name, []):
                if nid not in seen:
                    seen.append(nid)
            if seen:
                # nearest scope wins (compiler order); ambiguity only
                # within one scope level.
                cands = [nid for nid in
                         (ns_types.get(ns, {}) or {}).get(name, [])]
                return cands[0] if len(cands) == 1 else None
        return None

    extra: list[tuple[str, object]] = []

    def add_edge(owner_key: str, src: str, rel: str, tgt: str, src_row: dict,
                 etype: str, cclass: str, score, attrs: dict, site: str = ""):
        from ...graph.builder import carry_occurrences as _carry
        attrs = _carry(attrs, src_row)
        e = EdgeCls(canonical_edge_id(src, rel, tgt, owner_rel(owner_key),
                                      site),
                    src, tgt, rel, etype, cclass, score,
                    list(src_row.get("provenance", [])), METHOD, VERSION,
                    file_path=owner_rel(owner_key),
                    start_line=src_row.get("start_line") or 0,
                    attributes=attrs)
        extra.append((owner_key, e))
        return e

    prof.end("csharp_collect")
    # Defined before the import pass that calls them (they were defined
    # after it, so any namespace `using` raised UnboundLocalError and the
    # whole C# resolution for the repo was lost).
    def _file_dep(key, d, tgt_nid):
        t = _node(tgt_nid)
        if t is None:
            return
        tf = getattr(t, "file_path", "") or ""
        sf = owner_rel(key)
        if not tf or tf == sf:
            return
        add_edge(key, mod_id(sf), "depends_on", mod_id(tf), d,
                 "resolved", "resolved", 0.9,
                 {"specifier": _attrs(d).get("specifier", ""),
                  "via": "csharp-file-fact"},
                 site=_sf("csharpfiledep", sf, tf))
        diag["depends_on_derived"] += 1

    def _ns_dep(key, d, ns):
        files = sorted({(_node(nid).file_path or "")
                        for tbl in [ns_types.get(ns, {})]
                        for ids in tbl.values() for nid in ids
                        if _node(nid) is not None})
        files = [f for f in files if f and f != owner_rel(key)]
        if files:
            add_edge(key, mod_id(owner_rel(key)), "depends_on",
                     mod_id(files[0]), d, "resolved", "resolved", 0.9,
                     {"specifier": _attrs(d).get("specifier", ""),
                      "via": "csharp-ns-fact"},
                     site=_sf("csharpfiledep", owner_rel(key), files[0]))
            diag["depends_on_derived"] += 1

    prof.begin("csharp_bind")
    # (owner_key, local) -> ("ns", dotted) | ("type", node id)
    bindings: dict[tuple[str, str], tuple] = {}
    static_types: dict[str, list[str]] = {}  # owner_key -> `using static` type ids
    file_usings: dict[str, list[str]] = {}  # owner_key -> plain `using` namespaces
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = a.get("specifier", "")
        if not spec:
            continue
        if a.get("static_import"):
            # static using binds the TYPE (members resolve at call time)
            tgt = _find_type(spec.rsplit(".", 1)[-1],
                             file_ns.get(owner_rel(key), ""))
            if tgt is None:
                if spec.split(".")[0] in _STDLIB_ROOTS:
                    diag["external_imports"] += 1
                else:
                    diag["unresolved_imports"] += 1
                continue
            diag["resolved_imports"] += 1
            bindings[(key, spec.rsplit(".", 1)[-1])] = ("type", tgt)
            static_types.setdefault(key, []).append(tgt)
            _file_dep(key, d, tgt)
        elif a.get("alias"):
            tnode = _find_type(spec, file_ns.get(owner_rel(key), ""))
            if tnode is None:
                if spec.split(".")[0] in _STDLIB_ROOTS:
                    diag["external_imports"] += 1
                else:
                    diag["unresolved_imports"] += 1
                continue
            diag["resolved_imports"] += 1
            bindings[(key, a["alias"])] = ("type", tnode)
            _file_dep(key, d, tnode)
        else:
            # the repo's own namespace first: `Microsoft.eShopWeb.*` is not the BCL
            if spec not in ns_types and spec.split(".")[0] in _STDLIB_ROOTS:
                diag["external_imports"] += 1
                continue
            if spec in ns_types:
                diag["resolved_imports"] += 1
                bindings[(key, spec.rsplit(".", 1)[-1])] = ("ns", spec)
                _ns_dep(key, d, spec)
                file_usings.setdefault(key, []).append(spec)
            else:
                diag["unresolved_imports"] += 1
    # MSBuild implicit global usings: `<Using Include="A.B" Static="true"/>`
    # in a .csproj / Directory.Build.props applies to every file below it.
    proj_static = _project_static_usings(repo_root)
    if proj_static:
        for key in sorted({k for k, _ in unres_calls}):
            rel = owner_rel(key)
            for scope, spec in proj_static:
                if scope and not rel.startswith(scope + "/"):
                    continue
                ns_part, _, label = spec.rpartition(".")
                tgt = _find_type(label, ns_part)
                if tgt is not None and tgt not in static_types.get(key, []):
                    static_types.setdefault(key, []).append(tgt)
    prof.end("csharp_bind")
    prof.begin("csharp_calls")

    def _type_qnames(here_rel, label):
        """Qualified names of same-namespace types with this label."""
        out = []
        for f, tbl in file_syms.items():
            if file_ns.get(f, "") != file_ns.get(here_rel, ""):
                continue
            for nid in tbl.get(label, []) or []:
                n = _node(nid)
                if n is not None and getattr(n, "type", "") in (
                        "class", "interface", "type"):
                    out.append(getattr(n, "qualified_name", ""))
        return sorted(set(out))

    for key, d in unres_calls:
        a = _attrs(d)
        kind = a.get("callee_kind", "")
        tgt = None
        here = owner_rel(key)
        here_ns = file_ns.get(here, "")
        if kind == "function":
            base = a.get("base", "")
            owner_cls = _owner_class(d["source"])
            # a local function of the caller shadows the class's members
            src_qn = getattr(_node(d["source"]), "qualified_name", "") or ""
            cands = _methods_of(src_qn, base) if src_qn else []
            if not cands:
                cands = _methods_of(owner_cls, base) if owner_cls else []
            if not cands:
                for qn in _type_qnames(here, base):
                    cands.extend(_methods_of(qn, base))
            if not cands:
                # `using static ExpressionBuilder;` brings its members in
                for tid in static_types.get(key, []):
                    tq = getattr(_node(tid), "qualified_name", "") or ""
                    if tq:
                        cands.extend(_methods_of(tq, base))
            uniq = sorted(set(cands))
            tgt = uniq[0] if len(uniq) == 1 else None
            if tgt is None and len(uniq) > 1:
                diag["ambiguous_calls"] += 1
        elif kind in ("selector", "this", "super"):
            base = a.get("base", "")
            member = a.get("member", "")
            if kind == "this" or not base:
                owner_cls = _owner_class(d["source"])
                cands = _methods_of(owner_cls, member) if owner_cls else []
                uniq = sorted(set(cands))
                tgt = uniq[0] if len(uniq) == 1 else None
                if tgt is None and len(uniq) > 1:
                    diag["ambiguous_calls"] += 1
            elif base in ("base", "this"):
                tgt = None  # parent/this calls need heritage first (documented)
            else:
                b = bindings.get((key, base))
                tnode = None
                if b is not None and b[0] == "type":
                    tnode = b[1]
                else:
                    tnode = _find_type(base, here_ns, file_usings.get(key))
                if tnode is None and a.get("receiver_type"):
                    # `var basket = new Basket(..)` typed the local
                    rt = str(a["receiver_type"]).split("<")[0].rsplit(".", 1)[-1].strip()
                    if rt:
                        tnode = _find_type(rt, here_ns, file_usings.get(key))
                if tnode is not None:
                    n = _node(tnode)
                    qn = getattr(n, "qualified_name", "") if n else ""
                    meths = _methods_of(qn, member) if qn else []
                    tgt = meths[0] if len(meths) == 1 else None
                    if tgt is None and len(meths) > 1:
                        diag["ambiguous_calls"] += 1
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "csharp-resolve"},
                     site=_sf("csharpcall", a.get("base", ""),
                              a.get("member", "")))
    for key, d in heritage + instantiates:
        a = _attrs(d)
        raw = a.get("superclass") or a.get("interface") or \
            a.get("created") or ""
        short = (raw or "").split(".")[-1].split("<")[0].strip()
        if not short:
            continue
        tgt = _find_type(short, file_ns.get(owner_rel(key), ""), file_usings.get(key))
        if tgt and not tgt.startswith("ext:") and \
                not tgt.startswith("file:") and tgt != d["source"]:
            diag["resolved_heritage"] += 1
            add_edge(key, d["source"], d["relation"], tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "csharp-resolve", "name": short},
                     site=_sf("csharpheritage", short))
    prof.end("csharp_calls")
    prof.begin("csharp_files")
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "csharp")
    prof.end("csharp_files")
    return [], extra, diag


_SKIP_DIRS = frozenset({".git", "bin", "obj", "node_modules", ".ripplet", ".atlasgraph", ".vs"})


def _project_static_usings(repo_root: str) -> list[tuple[str, str]]:
    """(scope dir rel, `A.B.Type`) for every `<Using Include=".." Static="true"/>`
    in .csproj / Directory.Build.props files (scope "" = repo root)."""
    import os
    import re
    rx = re.compile(r"<Using\s+([^>]*?)/?>", re.I)
    out: list[tuple[str, str]] = []
    if not repo_root or not os.path.isdir(repo_root):
        return out
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fn in sorted(filenames):
            if not (fn.endswith(".csproj") or fn == "Directory.Build.props"):
                continue
            try:
                text = open(os.path.join(dirpath, fn), encoding="utf-8",
                            errors="ignore").read()
            except OSError:
                continue
            scope = os.path.relpath(dirpath, repo_root).replace("\\", "/")
            scope = "" if scope == "." else scope
            for m in rx.finditer(text):
                attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', m.group(1)))
                if attrs.get("Static", "").lower() == "true" and attrs.get("Include"):
                    out.append((scope, attrs["Include"].strip()))
    return out


def _emit_file_deps(extra, diag, store, new_results, mod_id, method,
                    version, tag):
    """File-level dependencies from resolved cross-file facts."""
    from ...core.identifiers import canonical_edge_id
    from ...core.identifiers import site_fingerprint as _sf2
    from ...core.models import Edge as EdgeCls
    seen: set[tuple[str, str, str]] = set()
    nodes: dict[str, object] = {}
    fresh = {getattr(m, "id", ""): m for _k, res in new_results
             for m in getattr(res, "nodes", [])}

    def _get(nid: str):
        if nid not in nodes:  # fresh-node scan per lookup was quadratic
            n = store.get_node(nid)
            nodes[nid] = n if n is not None else fresh.get(nid)
        return nodes[nid]

    for owner_key, e in list(extra):
        if e.relation not in ("calls", "inherits", "implements",
                              "instantiates"):
            continue
        s = _get(e.source)
        t = _get(e.target)
        if s is None or t is None:
            continue
        sf = getattr(s, "file_path", "") or ""
        tf = getattr(t, "file_path", "") or ""
        if not sf or not tf or sf == tf:
            continue
        if (owner_key, sf, tf) in seen:
            continue
        seen.add((owner_key, sf, tf))
        eid = canonical_edge_id(mod_id(sf), "depends_on", mod_id(tf),
                                owner_key.split(":", 1)[-1],
                                _sf2(f"{tag}filedep", sf, tf))
        extra.append((owner_key, EdgeCls(
            eid, mod_id(sf), mod_id(tf), "depends_on", "resolved",
            "resolved", 0.9, list(getattr(e, "provenance_ids", []) or []),
            method, version,
            file_path=owner_key.split(":", 1)[-1], start_line=0,
            attributes={"via": f"{tag}-file-fact"})))
        diag["depends_on_derived"] = diag.get("depends_on_derived", 0) + 1
