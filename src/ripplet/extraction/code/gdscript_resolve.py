"""GDScript file-scoped resolution (Phase 42 Wave 4).

Binds ``ts-gdscript`` placeholders: `preload()` specs to files
(`res://` strips to project-root-relative, plain paths resolve
same-dir-relative), `extends` bare names to repo-wide `class_name`
symbols (single only; natives and unknowns stay unresolved with
reasons), `extends "res://..."` paths to the target file's class,
bare calls to same-file functions, `Alias.member` to preload-alias
target methods, `Type.member` to `class_name` file methods, `self` to
own-class methods, `super` to superclass methods (via the extractor's
inherits placeholders), and `.new()` to instantiations. Single targets
only (stringly signal connect/emit, dynamic loads and `_G`-style
access are documented non-goals).

Conventions mirror ``java_resolve``: repo-scoped inputs, manifest-owned
outputs, method ``gdscript-resolve`` v1, deterministic sites, single
targets only.
"""
from __future__ import annotations
import posixpath

METHOD = "gdscript-resolve"
VERSION = "1"


def resolve_repo(repo_id: str, repo_root: str, file_set: set[str],
                 new_results: list, store, exclude_keys: set[str] | None = None,
                 fresh_keys: set[str] | None = None, prof=None) \
        -> tuple[list, list, dict]:
    """Returns ([], [(owner_key, Edge)], diagnostics) for one repo. See
    js_resolve.resolve_repo for the exclude/fresh contract."""
    exclude_keys = exclude_keys or set()
    fresh_rels = {k.split(":", 1)[-1] for k in (fresh_keys or set())}
    if prof is None:
        from ..evaluation.stageprofile import make_profiler as _mp
        prof = _mp(False)
    prof.begin("gdscript_collect")
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

    gd_files = sorted(f for f in file_set if f.lower().endswith(".gd"))

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

    imports: list = []
    unres_calls: list = []
    heritage: list = []
    instantiates: list = []
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
            if not (d["method"] or "").startswith("ts-gdscript"):
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
            if not (e.extraction_method or "").startswith("ts-gdscript"):
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

    file_syms: dict = {}
    # class_name -> file rel (repo-wide globals)
    class_files: dict = {}

    def _add_sym(rel: str, node):
        if getattr(node, "type", "") not in ("function", "method", "class",
                                             "interface", "type",
                                             "variable", "constant",
                                             "event"):
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

    def _class_label(rel: str) -> str:
        # The file-class carries {"file_class": True} (inner classes do
        # not); first flagged node wins deterministically.
        for label, nids in sorted((file_syms.get(rel, {}) or {}).items()):
            for nid in nids:
                n = _node(nid)
                if n is not None and getattr(n, "type", "") == "class" \
                        and (getattr(n, "attributes", "") or {}).get(
                            "file_class"):
                    return label
        return ""

    for f in gd_files:
        lab = _class_label(f)
        if lab:
            class_files.setdefault(lab, f)

    def _dir_of(rel: str) -> str:
        return rel.rpartition("/")[0]

    def _resolve_preload(from_rel: str, spec: str):
        spec = (spec or "").strip()
        if not spec:
            return None, ""
        if spec.startswith("res://"):
            cand = posixpath.normpath(spec[len("res://"):])
            if cand in file_set:
                return cand, "res"
            return None, ""
        here = _dir_of(from_rel)
        cand = posixpath.normpath(here + "/" + spec) if here else \
            posixpath.normpath(spec)
        if cand in file_set:
            return cand, "relative"
        if spec in file_set:
            return spec, "root-relative"
        return None, ""

    extra: list = []

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

    prof.end("gdscript_collect")
    prof.begin("gdscript_bind")
    bindings: dict = {}
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = a.get("specifier", "")
        if not spec:
            continue
        tgt, via = _resolve_preload(owner_rel(key), spec)
        if tgt is None:
            diag["unresolved_imports"] += 1
            continue
        diag["resolved_imports"] += 1
        const_name = ""
        src_node = _node(d["source"])
        if src_node is not None and getattr(src_node, "type", "") in (
                "constant", "variable"):
            const_name = getattr(src_node, "label", "") or ""
        if const_name:
            bindings.setdefault((key, const_name), tgt)
        add_edge(key, mod_id(owner_rel(key)), "depends_on", mod_id(tgt),
                 d, "resolved", "resolved", 0.9,
                 {"specifier": spec, "via": "gdscript-" + via},
                 site=_sf("gddep", spec, tgt))
        diag["depends_on_derived"] += 1
    prof.end("gdscript_bind")
    prof.begin("gdscript_calls")

    def _owner_label(node_id: str) -> str:
        n = _node(node_id)
        if n is None:
            return ""
        parts = (getattr(n, "qualified_name", "") or "").split(".")
        return parts[-2] if len(parts) >= 2 else ""

    def _file_methods(rel: str, member: str) -> list:
        out = []
        for nid in (file_syms.get(rel, {}) or {}).get(member, []):
            n = _node(nid)
            if n is not None and getattr(n, "type", "") == "method" and \
                    _owner_label(nid) == _class_label(rel):
                out.append(nid)
        return sorted(set(out))

    def _superclass_label(method_nid: str) -> str:
        mn = _node(method_nid)
        if mn is None:
            return ""
        mqn = getattr(mn, "qualified_name", "") or ""
        owner_qn = mqn.rpartition(".")[0]
        mfile = getattr(mn, "file_path", "") or ""
        for key2, hd in heritage:
            if owner_rel(key2) != mfile:
                continue
            hn = _node(hd["source"])
            if hn is None:
                continue
            if (getattr(hn, "qualified_name", "") or "") != owner_qn:
                continue
            ha = _attrs(hd)
            short = (ha.get("superclass") or "").split(".")[-1]
            if short and not ha.get("path"):
                return short
            if short and ha.get("path"):
                tgt = _resolve_preload(mfile, ha.get("superclass") or "")
                if tgt[0]:
                    return _class_label(tgt[0])
        return ""

    for key, d in unres_calls:
        a = _attrs(d)
        kind = a.get("callee_kind", "")
        here = owner_rel(key)
        tgt = None
        if kind == "function":
            base = a.get("base", "")
            cands = [nid for nid in
                     (file_syms.get(here, {}) or {}).get(base, [])
                     if (_node(nid) is not None and
                         getattr(_node(nid), "type", "") in
                         ("function", "method"))]
            uniq = sorted(set(cands))
            tgt = uniq[0] if len(uniq) == 1 else None
            if tgt is None and len(uniq) > 1:
                diag["ambiguous_calls"] += 1
        elif kind in ("selector", "this", "super"):
            base = a.get("base", "")
            member = a.get("member", "")
            if kind == "this" or (kind == "selector" and not base):
                cands = _file_methods(here, member)
                uniq = sorted(set(cands))
                tgt = uniq[0] if len(uniq) == 1 else None
                if tgt is None and len(uniq) > 1:
                    diag["ambiguous_calls"] += 1
            elif kind == "super":
                sup = _superclass_label(d["source"])
                if sup and sup in class_files:
                    cands = _file_methods(class_files[sup], member)
                elif sup:
                    cands = [nid for nid in
                             (file_syms.get(here, {}) or {}).get(member, [])
                             if _owner_label(nid) == sup]
                else:
                    cands = []
                uniq = sorted(set(cands))
                tgt = uniq[0] if len(uniq) == 1 else None
                if tgt is None and len(uniq) > 1:
                    diag["ambiguous_calls"] += 1
            else:
                tfile = bindings.get((key, base))
                if tfile is not None:
                    cands = _file_methods(tfile, member)
                    uniq = sorted(set(cands))
                    tgt = uniq[0] if len(uniq) == 1 else None
                    if tgt is None and len(uniq) > 1:
                        diag["ambiguous_calls"] += 1
                elif base in class_files:
                    cands = _file_methods(class_files[base], member)
                    uniq = sorted(set(cands))
                    tgt = uniq[0] if len(uniq) == 1 else None
                    if tgt is None and len(uniq) > 1:
                        diag["ambiguous_calls"] += 1
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "gdscript-resolve"},
                     site=_sf("gdcall", a.get("base", ""),
                              a.get("member", "")))
    for key, d in heritage + instantiates:
        a = _attrs(d)
        raw = a.get("superclass") or a.get("interface") or \
            a.get("created") or ""
        here = owner_rel(key)
        tgt = None
        if a.get("path"):
            tfile, _ = _resolve_preload(here, raw)
            if tfile:
                lab = _class_label(tfile)
                cands = [nid for nid in
                         (file_syms.get(tfile, {}) or {}).get(lab, [])
                         if _node(nid) is not None]
                if len(cands) == 1:
                    tgt = cands[0]
                elif len(cands) > 1:
                    diag["ambiguous_calls"] += 1
        else:
            short = (raw or "").split(".")[-1].strip()
            if short and short in class_files:
                cands = [nid for nid in
                         (file_syms.get(class_files[short], {}) or {}
                          ).get(short, [])]
                if len(cands) == 1:
                    tgt = cands[0]
                elif len(cands) > 1:
                    diag["ambiguous_calls"] += 1
        if tgt and not tgt.startswith("ext:") and \
                not tgt.startswith("file:") and tgt != d["source"]:
            diag["resolved_heritage"] += 1
            add_edge(key, d["source"], d["relation"], tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "gdscript-resolve", "name": raw},
                     site=_sf("gdheritage", raw or ""))
    prof.end("gdscript_calls")
    prof.begin("gdscript_files")
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "gdscript")
    prof.end("gdscript_files")
    return [], extra, diag


def _emit_file_deps(extra, diag, store, new_results, mod_id, method,
                    version, tag):
    """File-level dependencies from resolved cross-file facts (same
    contract as the other resolvers). Deterministic per file pair."""
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
