"""Dart library-scoped resolution (Phase 42 Wave 4).

Binds ``ts-dart`` placeholders: imports to files (`dart:` SDK stays
external; `package:` tries root-relative with and without the package
head plus same-dir joins; relative URIs resolve relatively; `as`
prefixes bind as aliases; `show`/`hide` combinators are recorded but
never bound — a filtered name must not resolve, documented
conservative), `part` directives merge library scope, same-scope free
functions, `Type.member` (static/factory) calls, `this`/`super` calls,
bare `Type(...)` instantiations, `super(...)` initializer
instantiations, and heritage (`extends`->inherits, `with`/`implements`
->implements). Single targets only (cascade/conditional shapes never
reach the resolver: the extractor leaves them unresolved).

Conventions mirror ``java_resolve``: repo-scoped inputs, manifest-owned
outputs, method ``dart-resolve`` v1, deterministic sites, single targets
only.
"""
from __future__ import annotations
import posixpath

METHOD = "dart-resolve"
VERSION = "2"  # v2: show/hide imports bind their names, package: URIs of other packages in the repo (pubspec name)


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
    prof.begin("dart_collect")
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

    dart_files = sorted(f for f in file_set if f.lower().endswith(".dart"))

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
            if not (d["method"] or "").startswith("ts-dart"):
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
            if not (e.extraction_method or "").startswith("ts-dart"):
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

    def _add_sym(rel: str, node):
        if getattr(node, "type", "") not in ("function", "method", "class",
                                             "interface", "type",
                                             "variable"):
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

    def _dir_of(rel: str) -> str:
        return rel.rpartition("/")[0]

    _pub_memo: list = []

    def _pub_roots() -> dict:
        """Package name -> repo-relative dir, from the repo's pubspec.yaml files."""
        if not _pub_memo:
            import os as _os
            import re as _re
            out: dict = {}
            for dirpath, dirnames, filenames in _os.walk(repo_root):
                dirnames[:] = [x for x in dirnames if not x.startswith(".") and x not in (
                    "build", "node_modules", "ios", "android", "macos", "windows", "linux", "web")]
                if "pubspec.yaml" in filenames:
                    try:
                        txt = open(_os.path.join(dirpath, "pubspec.yaml"), encoding="utf-8",
                                   errors="replace").read(4000)
                    except OSError:
                        continue
                    m = _re.search(r"^name:\s*([A-Za-z_][\w]*)", txt, _re.M)
                    rel_ = _os.path.relpath(dirpath, repo_root).replace("\\", "/")
                    if m and m.group(1) not in out:
                        out[m.group(1)] = "" if rel_ == "." else rel_
            _pub_memo.append(out)
        return _pub_memo[0]

    def _resolve_uri(from_rel: str, spec: str):
        """(target_rel or None, via, external?)."""
        spec = (spec or "").strip()
        if not spec:
            return None, "", False
        if spec.startswith("dart:"):
            return None, "sdk", True
        here = _dir_of(from_rel)
        if spec.startswith("package:"):
            rest = spec[len("package:"):]
            cands = [rest, rest.split("/", 1)[-1]]
            if here:
                cands.extend([here + "/" + c for c in list(cands)])
            for cand in cands:
                cand = posixpath.normpath(cand)
                if cand in file_set and cand.lower().endswith(".dart"):
                    via = "package" if not cand.startswith(here + "/") \
                        or not here else "package-relative"
                    return cand, via, False
            # `package:dio/src/utils.dart` from another package of the repo:
            # the directory whose pubspec.yaml is `name: dio`, then lib/
            pkg, _, path_ = rest.partition("/")
            root_ = _pub_roots().get(pkg)
            if root_ is not None:
                cand = posixpath.normpath(posixpath.join(root_, "lib", path_))
                if cand in file_set:
                    return cand, "package", False
            return None, "", False
        cand = posixpath.normpath(here + "/" + spec) if here else \
            posixpath.normpath(spec)
        if cand in file_set:
            return cand, "relative", False
        if spec in file_set:
            return spec, "root-relative", False
        return None, "", False

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

    prof.end("dart_collect")
    prof.begin("dart_bind")
    # (owner_key, alias-or-name) -> ("file", rel, member|None)
    bindings: dict = {}
    # owner_key -> [bare-scope files] (plain imports expose all names)
    bare_scope: dict = {}
    filtered: dict = {}   # owner_key -> [(file, "show"|"hide", names)]

    def _filtered_files(key: str, name: str) -> list:
        return [f for f, mode, names in filtered.get(key, ())
                if (name in names) == (mode == "show")]
    # part libraries: frozenset groups sharing one scope
    lib_groups: dict = {}

    def _lib_group(rel: str) -> set:
        if rel in lib_groups:
            return lib_groups[rel]
        group = {rel}
        # New results plus stored manifests (incremental runs resolve
        # parts from stored edges too).
        for key2, res2 in new_results:
            if owner_rel(key2) != rel:
                continue
            for e in getattr(res2, "edges", []):
                if e.relation == "imports" and \
                        (e.attributes or {}).get("part"):
                    spec = (e.attributes or {}).get("specifier", "")
                    tgt, _, _ = _resolve_uri(rel, spec)
                    if tgt:
                        group.add(tgt)
        for key2, d2 in imports:
            if owner_rel(key2) != rel or d2["relation"] != "imports":
                continue
            a2 = _attrs(d2)
            if a2.get("part"):
                tgt, _, _ = _resolve_uri(rel, a2.get("specifier", ""))
                if tgt:
                    group.add(tgt)
        lib_groups[rel] = group
        return group

    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        if a.get("part"):
            continue  # scope merge only, no dependency edge
        spec = a.get("specifier", "")
        if not spec:
            continue
        tgt, via, external = _resolve_uri(owner_rel(key), spec)
        if external:
            diag["external_imports"] += 1
            continue
        if tgt is None:
            diag["unresolved_imports"] += 1
            continue
        diag["resolved_imports"] += 1
        add_edge(key, mod_id(owner_rel(key)), "depends_on", mod_id(tgt),
                 d, "resolved", "resolved", 0.9,
                 {"specifier": spec, "via": "dart-" + via},
                 site=_sf("dartdep", spec, tgt))
        diag["depends_on_derived"] += 1
        if a.get("show_hide"):
            # `import 'utils.dart' show warningLog;`: only the shown names
            # (or all but the hidden ones) come into scope
            filtered.setdefault(key, []).append((tgt, a["show_hide"], set(a.get("names") or ())))
            continue
        alias = a.get("alias", "")
        if alias:
            bindings.setdefault((key, alias), ("file", tgt, None))
        else:
            bare_scope.setdefault(key, []).append(tgt)
            add_edge(key, mod_id(owner_rel(key)), "imports", mod_id(tgt),
                     d, "resolved", "resolved", 0.9,
                     {"via": "dart-bare", "specifier": spec},
                     site=_sf("dartimport", spec, tgt))
    prof.end("dart_bind")
    prof.begin("dart_calls")

    def _owner_label(node_id: str) -> str:
        n = _node(node_id)
        if n is None:
            return ""
        parts = (getattr(n, "qualified_name", "") or "").split(".")
        return parts[-2] if len(parts) >= 2 else ""

    _methods_memo: dict = {}

    def _methods_of(owner_label: str, member: str) -> list:
        _mk = (owner_label, member)
        if _mk in _methods_memo:
            return list(_methods_memo[_mk])
        out = []
        for f, tbl in file_syms.items():
            for nid in tbl.get(member, []) or []:
                n = _node(nid)
                if n is not None and getattr(n, "type", "") == "method" \
                        and _owner_label(nid) == owner_label:
                    out.append(nid)
        _methods_memo[_mk] = sorted(set(out))
        return list(_methods_memo[_mk])

    def _names_in(files: list, name: str, types: tuple) -> list:
        out = []
        for f in files:
            for nid in (file_syms.get(f, {}) or {}).get(name, []):
                n = _node(nid)
                if n is not None and getattr(n, "type", "") in types:
                    out.append(nid)
        return sorted(set(out))

    def _superclass_label(method_nid: str, here: str) -> str:
        mn = _node(method_nid)
        if mn is None:
            return ""
        mqn = getattr(mn, "qualified_name", "") or ""
        owner_qn = mqn.rpartition(".")[0]
        for key2, hd in heritage:
            hn = _node(hd["source"])
            if hn is None:
                continue
            if (getattr(hn, "qualified_name", "") or "") != owner_qn:
                continue
            ha = _attrs(hd)
            short = (ha.get("superclass") or "").split(".")[-1]
            if short:
                return short
        return ""

    for key, d in unres_calls:
        a = _attrs(d)
        kind = a.get("callee_kind", "")
        here = owner_rel(key)
        scope = set(_lib_group(here))
        for f in bare_scope.get(key, []) or []:
            scope.add(f)
        scope.update(_filtered_files(key, a.get("base", "")))
        tgt = None
        if kind == "function":
            base = a.get("base", "")
            owner_label = _owner_label(d["source"])
            cands = _methods_of(owner_label, base) if owner_label else []
            if not cands:
                cands = _names_in(sorted(scope), base,
                                  ("function", "method"))
            uniq = sorted(set(cands))
            tgt = uniq[0] if len(uniq) == 1 else None
            if tgt is None and len(uniq) > 1:
                diag["ambiguous_calls"] += 1
            if tgt is None and not uniq:
                cl = _names_in(sorted(scope), base,
                               ("class", "interface", "type"))
                if len(cl) == 1:
                    add_edge(key, d["source"], "instantiates", cl[0], d,
                             "resolved", "resolved", 0.9,
                             {"via": "dart-resolve", "name": base},
                             site=_sf("dartinst", base))
                    diag["resolved_heritage"] += 1
                elif len(cl) > 1:
                    diag["ambiguous_calls"] += 1
        elif kind in ("selector", "this", "super"):
            base = a.get("base", "")
            member = a.get("member", "")
            if kind == "this" or (kind == "selector" and not base):
                owner_label = _owner_label(d["source"])
                cands = _methods_of(owner_label, member) \
                    if owner_label else []
                uniq = sorted(set(cands))
                tgt = uniq[0] if len(uniq) == 1 else None
                if tgt is None and len(uniq) > 1:
                    diag["ambiguous_calls"] += 1
            elif kind == "super":
                sup = _superclass_label(d["source"], here)
                cands = _methods_of(sup, member) if sup else []
                uniq = sorted(set(cands))
                tgt = uniq[0] if len(uniq) == 1 else None
                if tgt is None and len(uniq) > 1:
                    diag["ambiguous_calls"] += 1
            else:
                b = bindings.get((key, base))
                if b is not None:
                    _tfile = b[1]
                    cands = _methods_of(base, member)
                    cands = [c for c in cands
                             if (_node(c) is not None and
                                 (getattr(_node(c), "file_path", "")
                                  or "") == _tfile)]
                    uniq = sorted(set(cands))
                    tgt = uniq[0] if len(uniq) == 1 else None
                    if tgt is None and len(uniq) > 1:
                        diag["ambiguous_calls"] += 1
                else:
                    cands = []
                    for nid in _names_in(sorted(scope), base,
                                         ("class", "interface", "type")):
                        n = _node(nid)
                        qn = (getattr(n, "qualified_name", "") or "")
                        cands.extend(
                            _methods_of(qn.split(".")[-1], member))
                    uniq = sorted(set(cands))
                    tgt = uniq[0] if len(uniq) == 1 else None
                    if tgt is None and len(uniq) > 1:
                        diag["ambiguous_calls"] += 1
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "dart-resolve"},
                     site=_sf("dartcall", a.get("base", ""),
                              a.get("member", "")))
    for key, d in heritage + instantiates:
        a = _attrs(d)
        raw = a.get("superclass") or a.get("interface") or \
            a.get("created") or ""
        if raw == "super":
            short = _superclass_label(d["source"], owner_rel(key))
        else:
            short = (raw or "").split(".")[-1].split("<")[0].strip()
        if not short:
            continue
        here = owner_rel(key)
        scope = set(_lib_group(here))
        for f in bare_scope.get(key, []) or []:
            scope.add(f)
        scope.update(_filtered_files(key, short))
        cands = _names_in(sorted(scope), short,
                          ("class", "interface", "type"))
        uniq = sorted(set(cands))
        tgt = uniq[0] if len(uniq) == 1 else None
        if tgt and not tgt.startswith("ext:") and \
                not tgt.startswith("file:") and tgt != d["source"]:
            diag["resolved_heritage"] += 1
            add_edge(key, d["source"], d["relation"], tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "dart-resolve", "name": short},
                     site=_sf("dartheritage", short))
    prof.end("dart_calls")
    prof.begin("dart_files")
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "dart")
    prof.end("dart_files")
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
