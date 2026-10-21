"""Java package-scoped resolution (Phase 39 Wave 1).

Binds ``ts-java`` placeholders: package clauses to directories
(dot-path suffix match; split packages with duplicate labels stay
ambiguous), single-type/static imports to symbols, on-demand `.*`
imports as namespaces (member lookup at call time, never shadowing),
same-package and same-class calls, `new X()` instantiations, and
same-package/imported heritage. `java.lang` is external-stdlib (real
auto-import semantics; the JDK is never in the repo). Object-variable
calls stay unresolved (no flow analysis).

Conventions mirror ``go_resolve``: repo-scoped inputs, manifest-owned
outputs, method ``java-resolve`` v1, deterministic sites, single
targets only.
"""
from __future__ import annotations

METHOD = "java-resolve"
VERSION = "7"  # v7: static Type.m() looks nested types of the enclosing classes up first, never the first of several; v6: package of a file = its package line (was cut by one part); v5: single/static imports of packages split over main+test dirs; v4: heritage through imports (single, wildcard, qualified); v3: static imports of Kotlin file-facade functions;  # v2: bare calls never bind to other classes of the package


def resolve_repo(repo_id: str, repo_root: str, file_set: set[str],
                 new_results: list, store, exclude_keys: set[str] | None = None,
                 fresh_keys: set[str] | None = None, prof=None,
                 peer_results: list | None = None) \
        -> tuple[list, list, dict]:
    exclude_keys = exclude_keys or set()
    fresh_rels = {k.split(":", 1)[-1] for k in (fresh_keys or set())}
    if prof is None:
        from ..evaluation.stageprofile import make_profiler as _mp
        prof = _mp(False)
    prof.begin("java_collect")
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

    java_files = sorted(f for f in file_set if f.lower().endswith(".java"))

    # ---- node lookup (stored + new results)
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

    # ---- collect ts-java edges
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
            if not (d["method"] or "").startswith("ts-java"):
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
            if not (e.extraction_method or "").startswith("ts-java"):
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
    # rel -> label -> [node ids] (methods share plain labels; owners
    # recovered from qualified names)
    file_pkg: dict[str, str] = {}
    # rel -> dotted package ("" = default package)
    pkg_files: dict[str, list[str]] = {}
    # dotted package -> [rels]

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

    def _pkg_of(rel: str) -> str:
        """The file's declared package. The module node is named by the
        `package` line; a file without one keeps its path-derived name (the
        default package). Stripping the last part (as for `pkg.File` names)
        turned `com.squareup.javapoet` into `com.squareup` (v6)."""
        qn = ""
        for key, res in new_results:
            if owner_rel(key) != rel:
                continue
            for n in getattr(res, "nodes", []):
                if getattr(n, "type", "") == "module":
                    qn = getattr(n, "qualified_name", "") or ""
                    break
            break
        else:
            n = _module_node(rel)
            qn = (getattr(n, "qualified_name", "") or "") if n is not None else ""
        path_name = rel.rsplit(".", 1)[0].replace("/", ".")
        return "" if not qn or qn == path_name else qn

    def _module_node(rel: str):
        for r in store.cx.execute(
                "SELECT id FROM nodes WHERE type='module' AND "
                "repository_id=?", (repo_id,)).fetchall():
            n = store.get_node(r["id"])
            if n is not None and (n.file_path or "") == rel:
                return n
        return None

    for f in java_files:
        p = _pkg_of(f)
        file_pkg[f] = p
        pkg_files.setdefault(p, []).append(f)

    def _owner_class(node_id: str) -> str:
        """Enclosing class qname of a method node, or ''."""
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
        for f, tbl in file_syms.items():
            for nid in tbl.get(member, []) or []:
                n = _node(nid)
                if n is not None and getattr(n, "type", "") == "method" and \
                        _owner_class(nid) == owner_qn:
                    out.append(nid)
        _methods_memo[_mk] = sorted(set(out))
        return list(_methods_memo[_mk])

    def _type_qnames(label: str, package: str) -> list[str]:
        """Qualified names of same-package types with this label."""
        out = []
        for f in pkg_files.get(package, []) or []:
            for nid in (file_syms.get(f, {}) or {}).get(label, []):
                n = _node(nid)
                if n is not None and getattr(n, "type", "") in (
                        "class", "interface", "type"):
                    out.append(getattr(n, "qualified_name", ""))
        return sorted(set(out))

    def _dir_of_package(pkg: str) -> str | None:
        """Dotted package -> unique repo dir suffix, else None."""
        if not pkg:
            return None
        cand = pkg.replace(".", "/")
        dirs = sorted({f.rpartition("/")[0] for f in java_files
                       if f.rpartition("/")[0] == cand or
                       f.rpartition("/")[0].endswith("/" + cand)})
        return dirs[0] if len(dirs) == 1 else None

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

    _kt_memo: dict = {}

    def _kotlin_facade_fn(spec: str):
        """`import static a.b.MoshiTesting.f` -> the top-level Kotlin `fun f`
        in package a.b whose file facade is MoshiTesting (`@file:JvmName`,
        `MoshiTesting.kt` or `MoshiTestingKt` for `MoshiTesting.kt`)."""
        parts = spec.split(".")
        if len(parts) < 3:
            return None
        pkg, facade, member = ".".join(parts[:-2]), parts[-2], parts[-1]
        if "fns" not in _kt_memo:
            rows = [(n.id, n.qualified_name, n.file_path)
                    for _k, res in list(new_results) + list(peer_results or [])
                    for n in getattr(res, "nodes", [])
                    if n.type == "function" and (n.file_path or "").endswith((".kt", ".kts"))]
            rows += store.cx.execute(
                "SELECT id, qualified_name, file_path FROM nodes WHERE type='function' "
                "AND language='kotlin'").fetchall()
            _kt_memo["fns"] = rows
        import os
        import re
        hits = set()
        for nid, q, fp in _kt_memo["fns"]:
            if q != f"{pkg}.{member}":
                continue
            stem = os.path.splitext(os.path.basename(fp or ""))[0]
            ok = facade in (stem, stem + "Kt")
            if not ok:
                try:
                    head = open(os.path.join(repo_root, fp), encoding="utf-8",
                                errors="ignore").read(2000)
                    ok = re.search(r'@file:JvmName\("' + re.escape(facade) + r'"\)', head) is not None
                except OSError:
                    ok = False
            if ok:
                hits.add(nid)
        return next(iter(hits)) if len(hits) == 1 else None

    prof.end("java_collect")
    prof.begin("java_bind")
    # (owner_key, local) -> ("file", f, member|None) | ("ns", dir) | None
    bindings: dict[tuple[str, str], tuple] = {}
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = a.get("specifier", "")
        if not spec:
            continue
        if a.get("glob_import"):
            dd = _dir_of_package(spec)
            if dd is None:
                diag["external_imports"] += 1
            else:
                diag["resolved_imports"] += 1
                bindings[(key, spec.rsplit(".", 1)[-1])] = ("ns", dd)
                anchor = sorted(f for f in java_files
                                if f.rpartition("/")[0] == dd)
                if anchor:
                    add_edge(key, mod_id(owner_rel(key)), "depends_on",
                             mod_id(anchor[0]), d, "resolved", "resolved",
                             0.9, {"specifier": spec, "via": "java-glob"},
                             site=_sf("javadep", spec, dd))
                    diag["depends_on_derived"] += 1
            continue
        if spec.startswith("java.lang."):
            diag["external_imports"] += 1
            continue
        member = spec.rsplit(".", 1)[-1]
        pkg = spec.rpartition(".")[0]
        dd = _dir_of_package(pkg)
        # a package split over src/main and src/test has two dirs: the
        # package's files are still known exactly
        if dd is None and not pkg_files.get(pkg) and a.get("static_import") and "." in pkg:
            # Static single-member: `pkg.Type.member` — the dir is the
            # package, the type only scopes the member (agent-found).
            parts = spec.split(".")
            member = parts[-1]
            pkg = ".".join(parts[:-2])
            dd = _dir_of_package(pkg)
        if dd is None and not pkg_files.get(pkg):
            kfn = _kotlin_facade_fn(spec) if a.get("static_import") else None
            if kfn is not None:  # package split over src/main + src/test dirs
                diag["resolved_imports"] += 1
                bindings[(key, spec.rsplit(".", 1)[-1])] = ("kfn", kfn)
                add_edge(key, mod_id(owner_rel(key)), "imports", kfn, d,
                         "resolved", "resolved", 0.9,
                         {"via": "java-kotlin-facade", "specifier": spec},
                         site=_sf("javaimport", spec, kfn))
                continue
            diag["external_imports"] += 1
            continue
        files = sorted(set(pkg_files.get(pkg, []) or []) |
                       {f for f in java_files if dd and f.rpartition("/")[0] == dd})
        cands = []
        for f in files:
            cands.extend((file_syms.get(f, {}) or {}).get(member, []))
        uniq = sorted(set(cands))
        if not uniq and a.get("static_import"):
            kfn = _kotlin_facade_fn(spec)
            if kfn is not None:
                diag["resolved_imports"] += 1
                bindings[(key, spec.rsplit(".", 1)[-1])] = ("kfn", kfn)
                add_edge(key, mod_id(owner_rel(key)), "imports", kfn, d,
                         "resolved", "resolved", 0.9,
                         {"via": "java-kotlin-facade", "specifier": spec},
                         site=_sf("javaimport", spec, kfn))
                continue
        if len(uniq) != 1:
            diag["unresolved_imports"] += 1
            continue
        tgt_file = next(f for f in files
                        if uniq[0] in (file_syms.get(f, {}).get(member, [])))
        diag["resolved_imports"] += 1
        add_edge(key, mod_id(owner_rel(key)), "depends_on",
                 mod_id(tgt_file), d, "resolved", "resolved", 0.9,
                 {"specifier": spec, "via": "java-single"},
                 site=_sf("javadep", spec, tgt_file))
        diag["depends_on_derived"] += 1
        local = member
        bindings[(key, local)] = ("file", tgt_file, member)
        add_edge(key, mod_id(owner_rel(key)), "imports", uniq[0], d,
                 "resolved", "resolved", 0.9,
                 {"via": "java-binding", "imported": member,
                  "specifier": spec},
                 site=_sf("javaimport", spec, member, uniq[0]))
    prof.end("java_bind")
    prof.begin("java_calls")

    def _same_package_types(here_rel: str, label: str) -> list[str]:
        return [qn for qn in _type_qnames(
            label, file_pkg.get(here_rel, ""))]

    for key, d in unres_calls:
        a = _attrs(d)
        kind = a.get("callee_kind", "")
        tgt = None
        here = owner_rel(key)
        if kind == "function":
            base = a.get("base", "")
            owner_cls = _owner_class(d["source"])
            cands = []
            if owner_cls:
                cands = _methods_of(owner_cls, base)
                # Java has no package-level functions: a bare `m()` is a
                # method of this class, an enclosing class (nested classes
                # see outer members), or a single static import. Searching
                # the whole package bound JUnit's `fail()` to an unrelated
                # `MethodSpecTest.Throws.fail` (precision pass v2).
                outer = owner_cls
                while not cands and "." in outer:
                    outer = outer.rsplit(".", 1)[0]
                    cands = _methods_of(outer, base)
            if not cands:
                b = bindings.get((key, base))
                if b is not None and b[0] == "kfn":
                    cands.append(b[1])
                if b is not None and b[0] == "file":
                    for nid in (file_syms.get(b[1], {}) or {}).get(base, []):
                        n = _node(nid)
                        if n is not None and getattr(n, "type", "") in (
                                "function", "method"):
                            cands.append(nid)
            uniq = sorted(set(cands))
            tgt = uniq[0] if len(uniq) == 1 else None
            if tgt is None and len(uniq) > 1:
                diag["ambiguous_calls"] += 1
        elif kind in ("selector", "this"):
            base = a.get("base", "")
            member = a.get("member", "")
            if kind == "this" or not base:
                owner_cls = _owner_class(d["source"])
                cands = _methods_of(owner_cls, member) if owner_cls else []
                uniq = sorted(set(cands))
                tgt = uniq[0] if len(uniq) == 1 else None
                if tgt is None and len(uniq) > 1:
                    diag["ambiguous_calls"] += 1
            else:
                b = bindings.get((key, base))
                if b is not None:
                    bkind = b[0]
                    if bkind == "ns":
                        cands = []
                        for f in java_files:
                            if f.rpartition("/")[0] == b[1]:
                                cands.extend((file_syms.get(f, {}) or {}).get(
                                    member, []))
                        uniq = sorted(set(cands))
                        tgt = uniq[0] if len(uniq) == 1 else None
                        if tgt is None and len(uniq) > 1:
                            diag["ambiguous_calls"] += 1
                    else:
                        _tfile, _bmember = b[1], b[2]
                        cands = (file_syms.get(_tfile, {}) or {}).get(
                            _bmember or member, [])
                        tgt = cands[0] if len(cands) == 1 else None
                        if tgt is None and len(cands) > 1:
                            diag["ambiguous_calls"] += 1
                else:
                    # `TestBean.verify(..)`: Java looks a simple type name up in
                    # the enclosing classes first (their nested types), then the
                    # package; several package candidates is ambiguous, never
                    # "the first one".
                    qns = list(_same_package_types(here, base))
                    sn = _node(d["source"])
                    parts = (getattr(sn, "qualified_name", "") or "").split(".")
                    lexical = [p for p in (".".join(parts[:i]) + "." + base for i in range(len(parts) - 1, 0, -1))
                               if p in qns]
                    if lexical:
                        qns = lexical[:1]
                    elif len(qns) > 1:
                        diag["ambiguous_calls"] += 1
                        qns = []
                    for qn in qns:
                        meths = _methods_of(qn, member)
                        if len(meths) == 1:
                            tgt = meths[0]
                        elif len(meths) > 1:
                            diag["ambiguous_calls"] += 1
                        break
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "java-resolve"},
                     site=_sf("javacall", a.get("base", ""),
                              a.get("member", "")))
    for key, d in heritage + instantiates:
        a = _attrs(d)
        raw = a.get("superclass") or a.get("interface") or \
            a.get("created") or ""
        short = (raw or "").split(".")[-1].split("<")[0].strip()
        if not short:
            continue
        tgt = None
        here = owner_rel(key)
        cands = []
        for f in pkg_files.get(file_pkg.get(here, ""), []) or []:
            for nid in (file_syms.get(f, {}) or {}).get(short, []):
                n = _node(nid)
                if n is not None and getattr(n, "type", "") in (
                        "class", "interface", "type"):
                    cands.append(nid)
        uniq = sorted(set(cands))
        tgt = uniq[0] if len(uniq) == 1 else None
        if tgt is None and not uniq:
            # not in the package: the class the file imports (`import a.b.C;`,
            # `import a.b.*;`) or a fully qualified `extends a.b.C`
            files_ = []
            b = bindings.get((key, short))
            if b is not None and b[0] == "file":
                files_ = [b[1]]
            elif "." in raw:
                pdir = _dir_of_package(raw.rsplit(".", 1)[0].split("<")[0])
                files_ = [f for f in java_files if pdir and f.rpartition("/")[0] == pdir]
            else:
                for (k_, _n), bb in bindings.items():
                    if k_ == key and bb[0] == "ns":
                        files_ += [f for f in java_files if f.rpartition("/")[0] == bb[1]]
            hc = sorted({nid for f in files_ for nid in (file_syms.get(f, {}) or {}).get(short, [])
                         if getattr(_node(nid), "type", "") in ("class", "interface", "type")})
            tgt = hc[0] if len(hc) == 1 else None
        if tgt and not tgt.startswith("ext:") and \
                not tgt.startswith("file:") and tgt != d["source"]:
            diag["resolved_heritage"] += 1
            add_edge(key, d["source"], d["relation"], tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "java-resolve", "name": short},
                     site=_sf("javaheritage", short))
    prof.end("java_calls")
    prof.begin("java_files")
    # File-level dependencies from resolved cross-file facts (Phase 39):
    # a call/heritage/instantiation that crosses files IS a file
    # dependency, import statement or not. Deterministic per file pair.
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "java")
    prof.end("java_files")
    return [], extra, diag


def _emit_file_deps(extra, diag, store, new_results, mod_id, method,
                    version, tag):
    """Shared file-level dependency derivation for resolved cross-file
    facts (used by java/rust/go resolvers with their own tags)."""
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
