"""Kotlin package-scoped resolution (Phase 41 Wave 3).

Binds ``ts-kotlin`` placeholders: package clauses to directories
(dot-path suffix match, Java doctrine), single imports to symbols,
on-demand `.*` imports as namespaces (member lookup at call time),
same-package and same-class calls, `this` calls, companion-object calls
(`Type.method` via the companion), constructor-call instantiations, and
same-package heritage. `kotlin.*` is external-stdlib (real auto-import
semantics; the stdlib is never in the repo). `as`-aliased imports are
NOT rebound (the plain name stays unbound: documented miss, never
misbound). Receiver-variable calls stay unresolved (no flow analysis).

Conventions mirror ``java_resolve``: repo-scoped inputs, manifest-owned
outputs, method ``kotlin-resolve`` v1, deterministic sites, single
targets only.
"""
from __future__ import annotations

METHOD = "kotlin-resolve"
VERSION = "6"  # v6: an import binds through the file's declared package (a package spans modules and source sets), not only a unique folder; v5: own distinctiveness (no Swift verb filter), Kotlin with*/coroutine stdlib names; v4: heritage through imports and star imports, distinctive repo-unique methods (inferred); v3: top-level funs of star-imported packages (all dirs of the package)


# Kotlin stdlib extension names a repo may also declare: never bound by the
# unique-name rule (the receiver is usually a stdlib collection / string)
_KT_STDLIB = frozenset("""
getOrPut getOrElse getOrDefault getOrNull isNullOrEmpty isNullOrBlank orEmpty firstOrNull
lastOrNull singleOrNull mapNotNull filterNotNull filterIsInstance associateBy associateWith
groupBy sortedBy sortedWith sortedByDescending forEachIndexed mapIndexed flatMap joinToString
toMutableList toMutableMap toMutableSet takeIf takeUnless also apply let run use withIndex
removeIf replaceFirst substringBefore substringAfter startsWith endsWith ifEmpty ifBlank
toTypedArray asSequence buildString buildList buildMap isNotEmpty isNotBlank
withContext withTimeout withTimeoutOrNull withLock withIndex withDefault withPermit
runBlocking runCatching getOrThrow onSuccess onFailure mapCatching launchIn collectLatest
""".split())


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
    prof.begin("kotlin_collect")
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

    kt_files = sorted(f for f in file_set
                      if f.lower().endswith((".kt", ".kts")))

    _fresh_nodes = {getattr(m, "id", ""): m for _k, res in new_results
                    for m in getattr(res, "nodes", [])}
    _node_memo: dict = {}

    def _node(nid: str):
        """Stored node, else this run's fresh node (memoized: on a cold
        index every lookup scanned every fresh node -- leakcanary 68 s)."""
        if nid not in _node_memo:
            n = store.get_node(nid)
            _node_memo[nid] = n if n is not None else _fresh_nodes.get(nid)
        return _node_memo[nid]

    # ---- collect ts-kotlin edges
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
            if not (d["method"] or "").startswith("ts-kotlin"):
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
            if not (e.extraction_method or "").startswith("ts-kotlin"):
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

    # ---- per-file symbol tables + package index (stored + new)
    file_syms: dict[str, dict[str, list[str]]] = {}
    file_pkg: dict[str, str] = {}
    pkg_files: dict[str, list[str]] = {}

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

    def _module_node(rel: str):
        for r in store.cx.execute(
                "SELECT id FROM nodes WHERE type='module' AND "
                "repository_id=?", (repo_id,)).fetchall():
            n = store.get_node(r["id"])
            if n is not None and (n.file_path or "") == rel:
                return n
        return None

    def _pkg_of(rel: str) -> str:
        for key, res in new_results:
            if owner_rel(key) != rel:
                continue
            for n in getattr(res, "nodes", []):
                if getattr(n, "type", "") == "module":
                    qn = getattr(n, "qualified_name", "") or ""
                    # Module qn IS the package for Kotlin (no file stem).
                    return qn
        n = _module_node(rel)
        if n is not None:
            return getattr(n, "qualified_name", "") or ""
        return ""

    for f in kt_files:
        p = _pkg_of(f)
        file_pkg[f] = p
        pkg_files.setdefault(p, []).append(f)

    def _owner_qn(node_id: str) -> str:
        n = _node(node_id)
        if n is None:
            return ""
        parts = (getattr(n, "qualified_name", "") or "").split(".")
        return ".".join(parts[:-1]) if len(parts) >= 2 else ""

    def _is_companion(nid: str) -> bool:
        n = _node(nid)
        return bool(n is not None and (getattr(n, "attributes", "")
                                       or {}).get("companion"))

    # Indexes built once (same facts the per-call scans read). The scans
    # walked every symbol of every file per call -- O(calls x symbols) node
    # lookups, 395 s on kotlinpoet (131 files); lookups are now O(1).
    _meth_idx: dict[tuple[str, str], list[str]] = {}
    _comp_idx: dict[str, list[str]] = {}
    _idx_built = [False]

    def _build_idx():
        if _idx_built[0]:
            return
        _idx_built[0] = True
        for _f, tbl in file_syms.items():
            for label, nids in tbl.items():
                for nid in nids:
                    n = _node(nid)
                    if n is None:
                        continue
                    if getattr(n, "type", "") == "method":
                        _meth_idx.setdefault((_owner_qn(nid), label), []).append(nid)
                    if _is_companion(nid):
                        cqn = getattr(n, "qualified_name", "") or ""
                        _comp_idx.setdefault(cqn.rpartition(".")[0], []).append(cqn)

    def _methods_of(owner_qn: str, member: str) -> list[str]:
        _build_idx()
        return sorted(set(_meth_idx.get((owner_qn, member), [])))

    def _companion_owners(qn: str) -> list[str]:
        """qn plus its companion-object scopes (Type.Companion, named)."""
        _build_idx()
        return sorted(set([qn] + _comp_idx.get(qn, [])))

    _tq_memo: dict = {}

    def _type_qnames(label: str, package: str) -> list[str]:
        if (label, package) not in _tq_memo:
            out = []
            for f in pkg_files.get(package, []) or []:
                for nid in (file_syms.get(f, {}) or {}).get(label, []):
                    n = _node(nid)
                    if n is not None and getattr(n, "type", "") in (
                            "class", "interface", "type"):
                        out.append(getattr(n, "qualified_name", ""))
            _tq_memo[(label, package)] = sorted(set(out))
        return list(_tq_memo[(label, package)])

    _kt_dirs = sorted({f.rpartition("/")[0] for f in kt_files})
    _dop_memo: dict = {}

    def _dir_of_package(pkg: str) -> str | None:
        if not pkg:
            return None
        if pkg not in _dop_memo:
            cand = pkg.replace(".", "/")
            dirs = [d for d in _kt_dirs if d == cand or d.endswith("/" + cand)]
            _dop_memo[pkg] = dirs[0] if len(dirs) == 1 else None
        return _dop_memo[pkg]

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

    prof.end("kotlin_collect")
    prof.begin("kotlin_bind")
    bindings: dict[tuple[str, str], tuple] = {}
    glob_dirs: dict[str, list] = {}   # owner_key -> star-imported package dirs
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = a.get("specifier", "")
        if not spec:
            continue
        if a.get("glob_import"):
            # a package may span several source sets / modules: all its dirs
            _c = spec.replace(".", "/")
            _gd = [d_ for d_ in _kt_dirs if d_ == _c or d_.endswith("/" + _c)]
            # ...and the folders of files that DECLARE this package, wherever they sit
            _gd += sorted({f_.rpartition("/")[0] for f_ in pkg_files.get(spec, []) or []} - set(_gd))
            glob_dirs.setdefault(key, []).extend(_gd)
            dd = _dir_of_package(spec)
            if dd is None:
                diag["external_imports"] += 1
            else:
                diag["resolved_imports"] += 1
                bindings[(key, spec.rsplit(".", 1)[-1])] = ("ns", dd)

                anchor = sorted(f for f in kt_files
                                if f.rpartition("/")[0] == dd)
                if anchor:
                    add_edge(key, mod_id(owner_rel(key)), "depends_on",
                             mod_id(anchor[0]), d, "resolved", "resolved",
                             0.9, {"specifier": spec,
                                   "via": "kotlin-glob"},
                             site=_sf("kotlindep", spec, dd))
                    diag["depends_on_derived"] += 1
            continue
        if spec == "kotlin" or spec.startswith("kotlin."):
            diag["external_imports"] += 1
            continue
        member = spec.rsplit(".", 1)[-1]
        pkg = spec.rpartition(".")[0]
        # the declared `package` is the truth: one package spans several source sets / modules / folders
        files = sorted(set(pkg_files.get(pkg, []) or []))
        if not files:
            dd = _dir_of_package(pkg)
            files = sorted(f for f in kt_files if f.rpartition("/")[0] == dd) if dd is not None else []
        if not files:
            diag["external_imports"] += 1
            continue
        cands = []
        for f in files:
            cands.extend((file_syms.get(f, {}) or {}).get(member, []))
        uniq = sorted(set(cands))
        if len(uniq) != 1:
            diag["unresolved_imports"] += 1
            continue
        tgt_file = next(f for f in files
                        if uniq[0] in (file_syms.get(f, {}).get(member, [])))
        diag["resolved_imports"] += 1
        add_edge(key, mod_id(owner_rel(key)), "depends_on",
                 mod_id(tgt_file), d, "resolved", "resolved", 0.9,
                 {"specifier": spec, "via": "kotlin-single"},
                 site=_sf("kotlindep", spec, tgt_file))
        diag["depends_on_derived"] += 1
        bindings[(key, member)] = ("file", tgt_file, member)
        add_edge(key, mod_id(owner_rel(key)), "imports", uniq[0], d,
                 "resolved", "resolved", 0.9,
                 {"via": "kotlin-binding", "imported": member,
                  "specifier": spec},
                 site=_sf("kotlinimport", spec, member, uniq[0]))
    prof.end("kotlin_bind")
    prof.begin("kotlin_calls")

    def _same_package_types(here_rel: str, label: str) -> list[str]:
        return [qn for qn in _type_qnames(
            label, file_pkg.get(here_rel, ""))]

    # (package, label) / (dir, label) -> node ids, built once: per-call
    # loops over every file of a package were quadratic (leakcanary: 20 s).
    _pkg_idx: dict = {}
    for _pk, _fs in pkg_files.items():
        for _f in _fs or []:
            for _lab, _ids in (file_syms.get(_f, {}) or {}).items():
                _pkg_idx.setdefault((_pk, _lab), []).extend(_ids)
    _dir_idx: dict = {}
    for _f in kt_files:
        for _lab, _ids in (file_syms.get(_f, {}) or {}).items():
            _dir_idx.setdefault((_f.rpartition("/")[0], _lab), []).extend(_ids)

    def _distinctive(name: str) -> bool:
        # camelCase of 2+ words, 8+ chars (Swift's generic-verb filter is for
        # Swift/Foundation names; Kotlin's stdlib names are listed instead)
        return len(name) >= 8 and any(ch.isupper() for ch in name[1:])
    _by_label: dict = {}
    for _f in kt_files:
        for _lab, _ids in (file_syms.get(_f, {}) or {}).items():
            for _nid in _ids:
                _n = _node(_nid)
                if _n is not None and getattr(_n, "type", "") == "method":
                    _by_label.setdefault(_lab, set()).add(_nid)

    def _methods_named(label: str) -> list[str]:
        return sorted(_by_label.get(label, ()))

    for key, d in unres_calls:
        a = _attrs(d)
        kind = a.get("callee_kind", "")
        tgt = None
        here = owner_rel(key)
        if kind == "function":
            base = a.get("base", "")
            owner_cls = _owner_qn(d["source"])
            cands = []
            if owner_cls:
                cands = _methods_of(owner_cls, base)
            if not cands:
                for nid in _pkg_idx.get((file_pkg.get(here, ""), base), []):
                    n = _node(nid)
                    if n is not None and getattr(n, "type", "") in (
                            "function", "method"):
                        cands.append(nid)
            uniq = sorted(set(cands))
            tgt = uniq[0] if len(uniq) == 1 else None
            if tgt is None and len(uniq) > 1:
                diag["ambiguous_calls"] += 1
            if tgt is None and not uniq:
                # Imported top-level fun (agent-found Phase 41):
                # `import com.shop.db.ping` + `ping()` binds through the
                # import instead of staying unresolved.
                b = bindings.get((key, base))
                if b is not None and b[0] == "file":
                    _tfile, _bmember = b[1], b[2]
                    if _bmember == base:
                        ic = (file_syms.get(_tfile, {}) or {}).get(
                            base, [])
                        iuniq = sorted(set(ic))
                        tgt = iuniq[0] if len(iuniq) == 1 else None
                        if tgt is None and len(iuniq) > 1:
                            diag["ambiguous_calls"] += 1
            if tgt is None and not uniq:
                # `import org.x.core.*` + `stringParam(..)`: a top-level fun of
                # a star-imported package
                gc = []
                for dd in glob_dirs.get(key, ()):
                    for f in kt_files:
                        if f.rpartition("/")[0] != dd:
                            continue
                        for nid in (file_syms.get(f, {}) or {}).get(base, []):
                            n = _node(nid)
                            if n is not None and getattr(n, "type", "") == "function":
                                gc.append(nid)
                guniq = sorted(set(gc))
                tgt = guniq[0] if len(guniq) == 1 else None
                if tgt is None and len(guniq) > 1:
                    diag["ambiguous_calls"] += 1
            if tgt is None:
                # Bare `Mover("x")` where Mover is a same-package class:
                # that IS construction (agent-found Phase 41 reports the
                # shape; the unresolved call stays, instantiation added).
                inst_cands = []
                for qn in _same_package_types(here, base):
                    for nid in _pkg_idx.get((file_pkg.get(here, ""), base), []):
                        n = _node(nid)
                        if n is not None and \
                                getattr(n, "qualified_name", "") == qn:
                            inst_cands.append(nid)
                iuniq = sorted(set(inst_cands))
                if len(iuniq) == 1:
                    add_edge(key, d["source"], "instantiates",
                             iuniq[0], d, "resolved", "resolved", 0.9,
                             {"via": "kotlin-resolve", "name": base},
                             site=_sf("kotlininst", base))
                    diag["resolved_heritage"] += 1
                elif len(iuniq) > 1:
                    diag["ambiguous_calls"] += 1
        elif kind in ("selector", "this"):
            base = a.get("base", "")
            member = a.get("member", "")
            if kind == "this" or not base:
                owner_cls = _owner_qn(d["source"])
                cands = _methods_of(owner_cls, member) if owner_cls else []
                uniq = sorted(set(cands))
                tgt = uniq[0] if len(uniq) == 1 else None
                if tgt is None and len(uniq) > 1:
                    diag["ambiguous_calls"] += 1
            else:
                b = bindings.get((key, base))
                if b is not None:
                    if b[0] == "ns":
                        cands = list(_dir_idx.get((b[1], member), []))
                        uniq = sorted(set(cands))
                        tgt = uniq[0] if len(uniq) == 1 else None
                        if tgt is None and len(uniq) > 1:
                            diag["ambiguous_calls"] += 1
                    else:
                        _tfile, _bmember = b[1], b[2]
                        if _bmember and _bmember != member:
                            # `import pkg.Store` + `Store.openDb()`: the
                            # honest target is the openDb METHOD, not the
                            # Store class (agent-found Phase 41). Companion
                            # scopes included; else unresolved (never the
                            # class: a calls edge must name something
                            # callable).
                            mcands = []
                            for nid in (file_syms.get(_tfile, {}) or {}
                                        ).get(_bmember, []):
                                n = _node(nid)
                                if n is None or getattr(n, "type", "") \
                                        not in ("class", "interface",
                                                "type"):
                                    continue
                                cqn = getattr(n, "qualified_name", "") \
                                    or ""
                                for owner in _companion_owners(cqn):
                                    mcands.extend(
                                        _methods_of(owner, member))
                            # ...plus the class's own direct methods
                            # (`_companion_owners` already includes the
                            # class itself).
                            muniq = sorted(set(mcands))
                            tgt = muniq[0] if len(muniq) == 1 else None
                            if tgt is None and len(muniq) > 1:
                                diag["ambiguous_calls"] += 1
                        else:
                            cands = (file_syms.get(_tfile, {}) or {}).get(
                                _bmember or member, [])
                            tgt = cands[0] if len(cands) == 1 else None
                            if tgt is None and len(cands) > 1:
                                diag["ambiguous_calls"] += 1
                else:
                    for qn in _same_package_types(here, base):
                        meths = []
                        for owner in _companion_owners(qn):
                            meths.extend(_methods_of(owner, member))
                        meths = sorted(set(meths))
                        if len(meths) == 1:
                            tgt = meths[0]
                            break
                        elif len(meths) > 1:
                            diag["ambiguous_calls"] += 1
                            break
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "kotlin-resolve"},
                     site=_sf("kotlincall", a.get("base", ""),
                              a.get("member", "")))
            continue
        # `merge(src) { whenMatchedDelete() }` (a lambda with receiver) /
        # `db.identifierManager.needQuotes(..)`: a distinctive name only one
        # repo method declares (inferred, like Swift/Ruby)
        name = a.get("member") or a.get("base", "")
        base = a.get("base", "") if a.get("member") else ""
        if kind not in ("function", "selector", "complex") or base[:1].isupper() \
                or not _distinctive(name) or name in _KT_STDLIB or a.get("local_callee") \
                or a.get("receiver_type") or a.get("chain_root_type"):
            continue
        owner_cls = _owner_qn(d["source"])
        if kind == "function" and (not a.get("implicit_member") or (owner_cls and _methods_of(owner_cls, name))):
            continue   # no typed context (an unparsed region) / the class's own method
        ms = _methods_named(name)
        if len(ms) == 1 and ms[0] != d["source"]:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", ms[0], d, "inferred", "inferred", 0.6,
                     {"via": "kotlin-unique-name"}, site=_sf("kotlinuniq", name))
    for key, d in heritage + instantiates:
        a = _attrs(d)
        raw = a.get("superclass") or a.get("interface") or \
            a.get("created") or ""
        short = (raw or "").split(".")[-1].split("<")[0].strip()
        if not short:
            continue
        here = owner_rel(key)
        cands = []
        for nid in _pkg_idx.get((file_pkg.get(here, ""), short), []):
            n = _node(nid)
            if n is not None and getattr(n, "type", "") in (
                    "class", "interface", "type"):
                cands.append(nid)
        if not cands:
            # `import org.x.core.SchemaUtilityApi` / `import org.x.core.*`
            b = bindings.get((key, short))
            files_ = [b[1]] if b is not None and b[0] == "file" else \
                [f for dd in glob_dirs.get(key, ()) for f in kt_files if f.rpartition("/")[0] == dd]
            for f in files_:
                for nid in (file_syms.get(f, {}) or {}).get(short, []):
                    n = _node(nid)
                    if n is not None and getattr(n, "type", "") in ("class", "interface", "type"):
                        cands.append(nid)
        uniq = sorted(set(cands))
        tgt = uniq[0] if len(uniq) == 1 else None
        if tgt and not tgt.startswith("ext:") and \
                not tgt.startswith("file:") and tgt != d["source"]:
            diag["resolved_heritage"] += 1
            add_edge(key, d["source"], d["relation"], tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "kotlin-resolve", "name": short},
                     site=_sf("kotlinheritage", short))
    prof.end("kotlin_calls")
    prof.begin("kotlin_files")
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "kotlin")
    prof.end("kotlin_files")
    return [], extra, diag


def _emit_file_deps(extra, diag, store, new_results, mod_id, method,
                    version, tag):
    """File-level dependencies from resolved cross-file facts (same
    contract as go/java/rust/cpp). Deterministic per file pair."""
    from ...core.identifiers import canonical_edge_id
    from ...core.identifiers import site_fingerprint as _sf2
    from ...core.models import Edge as EdgeCls
    seen: set[tuple[str, str, str]] = set()
    nodes: dict[str, object] = {}
    fresh = {getattr(m, "id", ""): m for _k, res in new_results
             for m in getattr(res, "nodes", [])}

    def _get(nid: str):
        if nid not in nodes:
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
