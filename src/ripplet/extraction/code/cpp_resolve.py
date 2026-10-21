"""C/C++ repo-scoped resolution (Phase 41 Wave 3).

Binds the placeholders ``ts-cpp``/``ts-c`` emit (one module serves both;
C is a structural subset): quoted includes to repo files (same-dir join,
then root-relative; system `<>` and `using namespace` stay external with
reasons), same-scope free functions, `Type::member` qualified calls,
`this` calls, `new T`/`T(...)` instantiations, and `base_class_clause`
heritage. Single targets only (overloads, virtual dispatch, ADL and
macro expansion are documented non-goals); declarations lose to
definitions exactly when the pair is unambiguous.

Conventions mirror ``java_resolve``: repo-scoped inputs (stored manifests
minus ghosts/fresh plus new results), manifest-owned outputs, method
``cpp-resolve`` v1, deterministic site fingerprints, no guessing.
"""
from __future__ import annotations
import posixpath

METHOD = "cpp-resolve"
VERSION = "5"  # v5: qualified calls fall back to a repo-unique qualified match; v4: quoted includes also found in parent dirs or as a unique repo path suffix; v3: repo headers via <..> includes (v2: transitive include scope)

_CPP_EXTS = (".cpp", ".cc", ".cxx", ".hpp", ".hxx", ".ipp", ".c", ".h")


def _is_cpp_method(method: str) -> bool:
    return method in ("ts-cpp", "ts-c")


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
    prof.begin("cpp_collect")
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

    cpp_files = sorted(f for f in file_set
                       if f.lower().endswith(_CPP_EXTS))

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

    # ---- collect ts-cpp/ts-c edges
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
            if not _is_cpp_method(d["method"] or ""):
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
            if not _is_cpp_method(e.extraction_method or ""):
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

    # ---- per-file symbol tables (stored + new)
    file_syms: dict[str, dict[str, list[str]]] = {}

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

    def _defined(nid: str) -> bool:
        n = _node(nid)
        return bool(n is not None and (getattr(n, "attributes", "")
                                       or {}).get("defined"))

    def _prefer_defined(cands: list[str]) -> str | None:
        uniq = sorted(set(cands))
        if len(uniq) == 1:
            return uniq[0]
        defd = [c for c in uniq if _defined(c)]
        if len(defd) == 1:
            return defd[0]
        return None

    def _owner_qn(node_id: str) -> str:
        n = _node(node_id)
        if n is None:
            return ""
        parts = (getattr(n, "qualified_name", "") or "").split(".")
        return ".".join(parts[:-1]) if len(parts) >= 2 else ""

    def _dir_of(rel: str) -> str:
        return rel.rpartition("/")[0]

    def _resolve_include(from_rel: str, spec: str):
        """(target_rel or None, via, note)."""
        spec = (spec or "").strip()
        if not spec:
            return None, "", "empty specifier"
        here = _dir_of(from_rel)
        cand = posixpath.normpath(here + "/" + spec) if here else \
            posixpath.normpath(spec)
        if cand in file_set:
            return cand, "relative", ""
        if spec in file_set:
            return spec, "root-relative", ""
        # `-I` include dirs are not known: try the parent dirs, then a single
        # repo file with that path suffix (`src/unix/internal.h` including
        # "uv-common.h" that lives in src/)
        d = here
        while d:
            d = _dir_of(d)
            cand = posixpath.normpath(d + "/" + spec) if d else posixpath.normpath(spec)
            if cand in file_set:
                return cand, "parent-dir", ""
        hits = [f for f in cpp_files if f.endswith("/" + spec)]
        if len(hits) == 1:
            return hits[0], "unique-suffix", ""
        return None, "", f"unresolved include: {spec}"

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

    prof.end("cpp_collect")
    prof.begin("cpp_bind")
    # include dirs reachable from each file (own dir + resolved targets)
    reachable: dict[str, set[str]] = {}

    inc_files: dict[str, set[str]] = {}  # file -> directly included repo files

    def _reachable(here: str) -> set[str]:
        """Dirs of `here` and of every file it includes, transitively: a
        test including `common.h` that includes `../lib.c` sees lib's dir."""
        if here in reachable:
            return reachable[here]
        seen, todo = {here}, [here]
        while todo:
            for t in inc_files.get(todo.pop(), ()):
                if t not in seen:
                    seen.add(t)
                    todo.append(t)
        dirs = {_dir_of(f) for f in seen}
        reachable[here] = dirs
        return dirs

    inc_targets: dict[str, list[str]] = {}
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = a.get("specifier", "")
        if not spec:
            continue
        if a.get("using"):
            diag["external_imports"] += 1
            continue
        if a.get("system"):
            # `#include <hiredis.h>` from the project's own examples/tests:
            # internal when the repo has exactly that header.
            tgt = spec if spec in file_set else None
            if tgt is None:
                hits = [f for f in cpp_files if f.endswith("/" + spec)]
                tgt = hits[0] if len(hits) == 1 else None
            via = "system-internal"
            if tgt is None:
                diag["external_imports"] += 1
                continue
        else:
            tgt, via, _note = _resolve_include(owner_rel(key), spec)
        if tgt is None:
            diag["unresolved_imports"] += 1
            continue
        diag["resolved_imports"] += 1
        inc_targets.setdefault(key, []).append(tgt)
        inc_files.setdefault(owner_rel(key), set()).add(tgt)
        add_edge(key, mod_id(owner_rel(key)), "depends_on", mod_id(tgt),
                 d, "resolved", "resolved", 0.9,
                 {"specifier": spec, "via": "cpp-" + via},
                 site=_sf("cppdep", spec, tgt))
        diag["depends_on_derived"] += 1
    prof.end("cpp_bind")
    prof.begin("cpp_calls")

    def _scope_files(here: str) -> list[str]:
        dirs = _reachable(here)
        return sorted(f for f in cpp_files if _dir_of(f) in dirs)

    def _free_funcs(here: str, name: str) -> list[str]:
        out = []
        for f in _scope_files(here):
            for nid in (file_syms.get(f, {}) or {}).get(name, []):
                n = _node(nid)
                if n is not None and getattr(n, "type", "") == \
                        "function":
                    out.append(nid)
        return out

    def _class_qnames(here: str, label: str) -> list[str]:
        out = []
        for f in _scope_files(here):
            for nid in (file_syms.get(f, {}) or {}).get(label, []):
                n = _node(nid)
                if n is not None and getattr(n, "type", "") in (
                        "class", "interface", "type"):
                    out.append(getattr(n, "qualified_name", ""))
        return sorted(set(out))

    _methods_memo: dict = {}

    def _methods_of(owner_qn: str, member: str) -> list[str]:
        _mk = (owner_qn, member)
        if _mk in _methods_memo:
            return list(_methods_memo[_mk])
        out = []
        for f, tbl in file_syms.items():
            for nid in tbl.get(member, []) or []:
                if _owner_qn(nid) == owner_qn:
                    n = _node(nid)
                    if n is not None and getattr(n, "type", "") == \
                            "method":
                        out.append(nid)
        _methods_memo[_mk] = sorted(set(out))
        return list(_methods_memo[_mk])

    for key, d in unres_calls:
        a = _attrs(d)
        kind = a.get("callee_kind", "")
        here = owner_rel(key)
        tgt = None
        if kind == "function":
            base = a.get("base", "")
            owner_cls = _owner_qn(d["source"])
            cands = _methods_of(owner_cls, base) if owner_cls else []
            if not cands:
                cands = _free_funcs(here, base)
            tgt = _prefer_defined(cands)
            if tgt is None and len(set(cands)) > 1:
                diag["ambiguous_calls"] += 1
        elif kind in ("selector", "qualified", "this"):
            base = a.get("base", "")
            member = a.get("member", "")
            if kind == "this" or not base:
                owner_cls = _owner_qn(d["source"])
                cands = _methods_of(owner_cls, member) if owner_cls \
                    else []
                tgt = _prefer_defined(cands)
                if tgt is None and len(set(cands)) > 1:
                    diag["ambiguous_calls"] += 1
            else:
                labels = [base.split(".")[-1]]
                cands = []
                for label in labels:
                    for qn in _class_qnames(here, label):
                        cands.extend(_methods_of(qn, member))
                if not cands and kind == "qualified":
                    # `os::filename_to_str(..)`: a namespace-qualified free
                    # function (relative to an enclosing namespace)
                    suffix = "." + base.replace("::", ".") + "." + member
                    scope_ = _scope_files(here)
                    # the included files first; a header reached only through
                    # other headers (args.h -> format.h -> base.h): the whole
                    # repo, accepted when the qualified name matches one symbol
                    for files_ in ((scope_, cpp_files) if scope_ else (cpp_files,)):
                        for f in files_:
                            for nid in (file_syms.get(f, {}) or {}).get(member, []):
                                n = _node(nid)
                                qn = getattr(n, "qualified_name", "") if n is not None else ""
                                if getattr(n, "type", "") == "function" and \
                                        ("." + qn).endswith(suffix):
                                    cands.append(nid)
                        if cands:
                            break
                tgt = _prefer_defined(cands)
                if tgt is None and len(set(cands)) > 1:
                    diag["ambiguous_calls"] += 1
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "cpp-resolve"},
                     site=_sf("cppcall", a.get("base", ""),
                              a.get("member", "")))
    for key, d in heritage + instantiates:
        a = _attrs(d)
        raw = a.get("superclass") or a.get("interface") or \
            a.get("created") or ""
        short = (raw or "").split(".")[-1].split("<")[0].strip()
        if not short:
            continue
        here = owner_rel(key)
        cands = []
        for qn in _class_qnames(here, short):
            for f in _scope_files(here):
                cands.extend(n for n in
                             (file_syms.get(f, {}) or {}).get(short, [])
                             if _node(n) is not None and
                             getattr(_node(n), "qualified_name", "") ==
                             qn)
        tgt = _prefer_defined(cands)
        if tgt is None and len(set(cands)) > 1:
            diag["ambiguous_calls"] += 1
        if tgt and not tgt.startswith("ext:") and \
                not tgt.startswith("file:") and tgt != d["source"]:
            diag["resolved_heritage"] += 1
            add_edge(key, d["source"], d["relation"], tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "cpp-resolve", "name": short},
                     site=_sf("cppheritage", short))
    prof.end("cpp_calls")
    prof.begin("cpp_files")
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "cpp")
    prof.end("cpp_files")
    return [], extra, diag


def _emit_file_deps(extra, diag, store, new_results, mod_id, method,
                    version, tag):
    """File-level dependencies from resolved cross-file facts (same
    contract as go/java/rust: a call/heritage/instantiation that crosses
    files IS a file dependency). Deterministic per file pair."""
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
