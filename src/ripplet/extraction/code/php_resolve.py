"""PHP namespace-scoped resolution (Phase 40 Wave 2).

Binds ``ts-php`` placeholders: namespace `use` singles (class/function
kinds; const kinds bind the file only — const values are not symbols),
group uses (expanded upstream), same-namespace symbols, `$this`/`self`/
`static` calls, `parent::` via resolved heritage (computed first),
`Class::member` via same-namespace/use-bound classes, and `new X()`
instantiations. Trait-`use` minted nothing upstream and binds nothing
here. Includes are skipped (dynamic paths, documented).

Conventions mirror ``go_resolve``: repo-scoped inputs, manifest-owned
outputs, method ``php-resolve`` v1, deterministic sites, single
targets only.
"""
from __future__ import annotations

METHOD = "php-resolve"
VERSION = "6"  # v6: unqualified function calls fall back to the global namespace; v2: owner class of namespaced methods


def resolve_repo(repo_id: str, repo_root: str, file_set: set[str],
                 new_results: list, store, exclude_keys: set[str] | None = None,
                 fresh_keys: set[str] | None = None, prof=None) \
        -> tuple[list, list, dict]:
    exclude_keys = exclude_keys or set()
    fresh_rels = {k.split(":", 1)[-1] for k in (fresh_keys or set())}
    if prof is None:
        from ..evaluation.stageprofile import make_profiler as _mp
        prof = _mp(False)
    prof.begin("php_collect")
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

    # ---- collect ts-php edges
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
            if not (d["method"] or "").startswith("ts-php"):
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
            if not (e.extraction_method or "").startswith("ts-php"):
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

    # ---- node lookup + FQCN index (backslash namespaces)
    # Memoized lookups (perf, identical results): every resolver loop below
    # called store.get_node per item and scanned new_results linearly.
    _new_nodes: dict = {}
    for _k, res in new_results:
        for m in getattr(res, "nodes", []):
            _new_nodes.setdefault(getattr(m, "id", ""), m)
    _node_cache: dict = {}

    def _node(nid: str):
        if nid in _node_cache:
            return _node_cache[nid]
        n = store.get_node(nid)
        if n is None:
            n = _new_nodes.get(nid)
        _node_cache[nid] = n
        return n

    fqn_file: dict[str, list[str]] = {}
    # backslash qname -> [file rels] (classes/interfaces/traits/types)
    fqn_id: dict[str, list[str]] = {}
    # backslash qname -> [node ids]
    file_ns: dict[str, str] = {}
    # rel -> backslash namespace ("" = global)
    file_syms: dict[str, dict[str, list[str]]] = {}
    # rel -> label -> [node ids] (methods share plain labels)

    def _add_sym(rel: str, node):
        if getattr(node, "type", "") not in ("function", "method", "class",
                                             "interface", "type"):
            return
        qn = getattr(node, "qualified_name", "") or ""
        file_syms.setdefault(rel, {}).setdefault(
            getattr(node, "label", ""), []).append(node.id)
        if "\\" in qn:
            ns = qn.rpartition("\\")[0]
            if rel and rel not in file_ns:
                file_ns[rel] = ns
            # All nominal kinds indexed (callers filter by type).
            fqn_file.setdefault(qn, []).append(rel)
            fqn_id.setdefault(qn, []).append(node.id)
        elif qn and getattr(node, "type", "") == "function":
            # global-namespace function (helpers files): reachable from any
            # namespace by its bare name (PHP's global fallback)
            fqn_file.setdefault(qn, []).append(rel)
            fqn_id.setdefault(qn, []).append(node.id)

    for key in repo_keys:
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is not None and (n.file_path or "") == owner_rel(key):
                _add_sym(owner_rel(key), n)
    for key, res in new_results:
        for n in getattr(res, "nodes", []):
            if (getattr(n, "file_path", "") or "") == owner_rel(key):
                _add_sym(owner_rel(key), n)

    def _owner_class(node_id: str) -> str:
        n = _node(node_id)
        if n is None or getattr(n, "type", "") != "method":
            return ""
        qn = getattr(n, "qualified_name", "") or ""
        # `Ns\Sub\Class.method` -> `Ns\Sub\Class` (the class, never the
        # namespace: that lost every $this/self call in namespaced code).
        return qn.rpartition(".")[0]

    def _methods_of(owner_qn: str, member: str) -> list[str]:
        out = []
        for rel, tbl in _file_methods(owner_qn).items():
            out.extend(tbl.get(member, []) or [])
        return sorted(set(out))

    _fm_idx: dict = {}
    _fm_built = [False]

    def _fm_add(owner: str, rel: str, t) -> None:
        _fm_idx.setdefault(owner, {}).setdefault(rel, {}).setdefault(
            getattr(t, "label", ""), []).append(t.id)

    def _file_methods(owner_qn: str) -> dict:
        """file rel -> {member: [ids]} for one owner class qname.

        Built once (it re-read every manifest edge with one SQL query per
        edge, per call: 481 s for a one-file update of guzzle). Keys and
        iteration order are the old per-call filters, so results are equal."""
        if not _fm_built[0]:
            _fm_built[0] = True
            for key in repo_keys:
                for eid in store.get_manifest_edges(key):
                    row = store.cx.execute("SELECT * FROM edges WHERE id=?",
                                           (eid,)).fetchone()
                    if row is None or row["relation"] != "defines":
                        continue
                    t = _node(row["target"])
                    if t is None or getattr(t, "type", "") != "method":
                        continue
                    qn = getattr(t, "qualified_name", "") or ""
                    for owner in dict.fromkeys((qn.rpartition(".")[0], qn,
                                                qn.rpartition("\\")[0])):
                        _fm_add(owner, owner_rel(key), t)
            for key, res in new_results:
                ids: dict = {}
                for n in getattr(res, "nodes", []):
                    ids.setdefault(getattr(n, "id", ""), n)  # first wins, as before
                for e in res.edges:
                    if e.relation != "defines":
                        continue
                    t = ids.get(e.target)
                    if t is None or getattr(t, "type", "") != "method":
                        continue
                    qn = getattr(t, "qualified_name", "") or ""
                    for owner in dict.fromkeys((qn.rpartition("\\")[0],
                                                qn.rpartition(".")[0])):
                        _fm_add(owner, owner_rel(key), t)
        return _fm_idx.get(owner_qn, {})

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

    prof.end("php_collect")
    prof.begin("php_bind")
    # (owner_key, local) -> ("file", rel, member|None, kind)
    bindings: dict[tuple[str, str], tuple] = {}
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = (a.get("specifier", "") or "").replace("/", "\\")
        if not spec:
            continue
        kind = a.get("use_kind", "class")
        local = (a.get("names", []) or [None])[0]
        if kind == "const" or not local:
            # Const values are not symbols; alias-less shapes bind the
            # file at most (never guessed).
            diag["unresolved_imports"] += 1
            continue
        files = fqn_file.get(spec, [])
        uniqf = sorted(set(files))
        if len(uniqf) != 1:
            diag["unresolved_imports"] += 1
            continue
        diag["resolved_imports"] += 1
        bindings[(key, local)] = (uniqf[0], local if kind == "function"
                                  else None, kind)
        add_edge(key, mod_id(owner_rel(key)), "depends_on",
                 mod_id(uniqf[0]), d, "resolved", "resolved", 0.9,
                 {"specifier": spec, "via": "php-use"},
                 site=_sf("phpdep", spec, uniqf[0]))
        diag["depends_on_derived"] += 1
        if kind == "function":
            for nid in fqn_id.get(spec, []) or []:
                n = _node(nid)
                if n is not None and getattr(n, "type", "") == "function":
                    add_edge(key, mod_id(owner_rel(key)), "imports", nid,
                             d, "resolved", "resolved", 0.9,
                             {"via": "php-binding", "imported": local,
                              "specifier": spec},
                             site=_sf("phpimport", spec, local, nid))
                    break
    prof.end("php_bind")
    prof.begin("php_heritage")
    def _bind_type(key, here_rel, short):
        ns = file_ns.get(here_rel, "")
        cands = []
        for cand_ns in ([ns] if ns else []) + [""]:
            qn = f"{cand_ns}\\{short}" if cand_ns else short
            for nid in fqn_id.get(qn, []) or []:
                n = _node(nid)
                if n is not None and getattr(n, "type", "") in (
                        "class", "interface", "type"):
                    cands.append(nid)
        uniq = sorted(set(cands))
        return uniq[0] if len(uniq) == 1 else None

    # Heritage first: parent:: calls need it below.
    for key, d in heritage:
        a = _attrs(d)
        raw = a.get("superclass") or a.get("interface") or ""
        short = (raw or "").split("\\")[-1].strip()
        if not short:
            continue
        tgt = None
        b = bindings.get((key, short))
        if b is not None and b[0] is not None:
            # `use Slim\Routing\RouteCollectorProxy;` ... `extends RouteCollectorProxy`
            ids = [nid for nid in (file_syms.get(b[0], {}) or {}).get(short, [])
                   if getattr(_node(nid), "type", "") in ("class", "interface")]
            tgt = ids[0] if len(ids) == 1 else None
        tgt = tgt or _bind_type(key, owner_rel(key), short)
        if tgt and not tgt.startswith("ext:") and \
                not tgt.startswith("file:") and tgt != d["source"]:
            diag["resolved_heritage"] += 1
            add_edge(key, d["source"], d["relation"], tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "php-resolve", "name": short},
                     site=_sf("phpheritage", short))

    prof.end("php_heritage")
    prof.begin("php_calls")

    def _qn_of(nid: str) -> str:
        n = _node(nid)
        return getattr(n, "qualified_name", "") or "" if n else ""

    _parent_idx: dict = {}
    _heritage_all: dict = {}
    _parent_seen = [0]

    def _parent_class(source_id: str) -> str:
        """Qualified name of the heritage parent (resolved edges only).
        First match in `extra` order, indexed incrementally as it grows."""
        for _ok, e in extra[_parent_seen[0]:]:
            # `extends` wins over `implements`: parent:: names the superclass
            if e.relation == "inherits":
                _parent_idx[e.source] = e.target
            elif e.relation == "implements":
                _parent_idx.setdefault(e.source, e.target)
        _parent_seen[0] = len(extra)
        # The call's source is a method: look up the heritage of its class.
        cls = [nid for nid in fqn_id.get(_owner_class(source_id), []) or []
               if getattr(_node(nid), "type", "") == "class"]
        t = _parent_idx.get(cls[0]) if len(cls) == 1 else _parent_idx.get(source_id)
        return _qn_of(t) if t else ""

    def file_method_labels(tfile: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for nid in sum(((file_syms.get(tfile, {}) or {}).values()), []):
            n = _node(nid)
            if n is not None and getattr(n, "type", "") == "method":
                out.setdefault(getattr(n, "label", ""), []).append(nid)
        return out

    for key, d in unres_calls:
        a = _attrs(d)
        kind = a.get("callee_kind", "")
        tgt = None
        here = owner_rel(key)
        ns = file_ns.get(here, "")
        if kind == "function":
            base = a.get("base", "")
            b = bindings.get((key, base))
            if b is not None and b[0] is not None:
                # Use-bound (class or function): the target file is
                # known; match the bare name among its function nodes.
                ids = [nid for nid in
                       (file_syms.get(b[0], {}) or {}).get(base, [])
                       if getattr(_node(nid), "type", "") == "function"]
                if len(ids) == 1:
                    tgt = ids[0]
                elif len(ids) > 1:
                    diag["ambiguous_calls"] += 1
            if tgt is None:
                # PHP rule for an unqualified function call: the current
                # namespace first, then the global one (`mock_function()` in
                # `namespace App\Tests;` runs the global function). A leading
                # backslash is fully qualified: that name only.
                if base.startswith("\\"):
                    order = [base.lstrip("\\")]
                else:
                    order = [f"{ns}\\{base}", base] if ns else [base]
                for qn in order:
                    ids = [nid for nid in fqn_id.get(qn, []) or []
                           if getattr(_node(nid), "type", "") == "function"]
                    if len(ids) == 1:
                        tgt = ids[0]
                    elif len(ids) > 1:
                        diag["ambiguous_calls"] += 1
                    if tgt:
                        break
        elif kind in ("this", "static"):
            base = a.get("base", "")
            member = a.get("member", "")
            owner_cls = ""
            if kind == "this":
                owner_cls = _owner_class(d["source"])
            elif base in ("self", "static"):
                owner_cls = _owner_class(d["source"])
            elif base == "parent":
                owner_cls = _parent_class(d["source"])
            elif base:
                # Use-bound names first (imported symbols shadow nothing
                # but resolve textually), then same-namespace types.
                owner_cls = ""
                b = bindings.get((key, base))
                if b is not None and b[0] is not None:
                    tfile = b[0]
                    meths = [nid for nid in
                             (file_method_labels(tfile).get(member, []) or [])]
                    if len(meths) == 1:
                        tgt = meths[0]
                if tgt is None:
                    bnode = _bind_type(key, here, base)
                    owner_cls = _qn_of(bnode) if bnode else ""
            if owner_cls:
                meths = _methods_of(owner_cls, member)
                if not meths:
                    # inherited: `$this->getErrorTitle()` defined on a parent class
                    # or from a used trait: walk every resolved heritage target
                    _parent_class(d["source"])          # refresh the heritage index
                    if not _heritage_all:
                        for _k, e in extra:
                            if e.relation in ("inherits", "implements"):
                                _heritage_all.setdefault(e.source, []).append(e.target)
                    queue, seen_q = [owner_cls], {owner_cls}
                    while queue and not meths and len(seen_q) < 24:
                        cls_q = queue.pop(0)
                        ids = [nid for nid in fqn_id.get(cls_q, []) or []
                               if getattr(_node(nid), "type", "") in ("class", "trait", "interface")]
                        for cid in ids[:1]:
                            for par in _heritage_all.get(cid, []):
                                pq = _qn_of(par)
                                if pq and pq not in seen_q:
                                    seen_q.add(pq)
                                    meths = meths or _methods_of(pq, member)
                                    queue.append(pq)
                tgt = meths[0] if len(meths) == 1 else None
                if tgt is None and len(meths) > 1:
                    diag["ambiguous_calls"] += 1
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "php-resolve"},
                     site=_sf("phppcall", a.get("base", ""),
                              a.get("member", "")))
    for key, d in instantiates:
        a = _attrs(d)
        short = (a.get("created", "") or "").split("\\")[-1]
        if not short:
            continue
        tgt = None
        # Use-bound first (agent-found: `new Order()` with
        # `use App\Models\Order` stayed ext:), then same-namespace.
        b = bindings.get((key, short))
        if b is not None and b[0] is not None:
            ids = [nid for nid in
                   (file_syms.get(b[0], {}) or {}).get(short, [])
                   if getattr(_node(nid), "type", "") in (
                       "class", "interface", "type")]
            if len(ids) == 1:
                tgt = ids[0]
            elif len(ids) > 1:
                diag["ambiguous_calls"] += 1
        if tgt is None:
            tgt = _bind_type(key, owner_rel(key), short)
        if tgt and not tgt.startswith("ext:") and \
                not tgt.startswith("file:") and tgt != d["source"]:
            diag["resolved_heritage"] += 1
            add_edge(key, d["source"], d["relation"], tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "php-resolve", "name": short},
                     site=_sf("phpheritage", short))
    prof.end("php_calls")
    prof.begin("php_files")
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "php")
    prof.end("php_files")
    return [], extra, diag


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
