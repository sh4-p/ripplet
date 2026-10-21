"""Swift module-scoped resolution (Phase 42 Wave 4).

Binds ``ts-swift`` placeholders: imports to directories (Swift modules
have no in-source declaration — the target name is unknowable — so a
`Foundation`-style stdlib list stays external, other modules resolve by
directory-suffix match, the rest stay unresolved with reasons;
same-directory files always bind), same-scope free functions, static
`Type.member` calls, `this`/`super` calls, `Type(...)` instantiations,
and heritage. Single targets only (extensions, overloads, generics and
protocol dispatch are documented non-goals).

Conventions mirror ``java_resolve``: repo-scoped inputs, manifest-owned
outputs, method ``swift-resolve`` v1, deterministic sites, single
targets only.
"""
from __future__ import annotations

METHOD = "swift-resolve"
VERSION = "6"  # v6: typed field paths left to the binder; v5: stricter distinctiveness; v4: distinctive repo-unique methods on untyped receivers (inferred); v3: Source/<M> modules; a package-unique top-level func when imports do not reach it; v2: SwiftPM module subtrees, imported-module free functions

# Apple SDK modules: certainly not repo code. Anything else bare and
# unmatched stays unresolved (a module may be internal: the target
# manifest is unmodeled, so external would be a guess).
_STDLIB = frozenset("""
Foundation Dispatch SwiftUI UIKit AppKit Combine CoreData CoreGraphics
CoreFoundation CoreLocation CoreMotion CoreAudio CoreVideo CoreImage
CoreText CoreML CreateML NaturalLanguage Speech AVFoundation AVKit
CloudKit Contacts EventKit HealthKit HomeKit MapKit MediaPlayer Messages
Network Security SafariServices StoreKit WatchKit WidgetKit SwiftData
Observation FoundationNetworking XCTest XCUITest Playgrounds RegexBuilder
_Concurrency Distributed Synchronization CxxStdlib Glibc Musl CRT WinSDK
""".split())


# Swift / Foundation member names a repo extension may also declare: never
# bound by the unique-name rule (the receiver is usually the library's)
_COMMON = frozenset("""
replacingOccurrences trimmingCharacters components appending appendingPathComponent
addingPercentEncoding compactMap flatMap forEach enumerated firstIndex lastIndex
contains removeAll removeFirst removeLast insertContentsOf withUnsafeBytes
withUnsafeMutableBytes withCheckedContinuation withCheckedThrowingContinuation
dropFirst dropLast hasPrefix hasSuffix lowercased uppercased isEmpty
""".split())


_GENERIC_HEADS = frozenset({"with", "get", "set", "make", "add", "remove", "update", "to", "map",
                            "for", "on", "did", "will", "should", "can", "has", "is", "open",
                            "close", "load", "save", "read", "write", "next", "find", "fetch"})


def _distinctive(name: str) -> bool:
    """A camelCase name of 3+ words (`isMemberOfATestClass`), or of two words
    when long and not a generic verb form (`containsNewlines`, but not
    `withValue` / `openScope`, which libraries declare too)."""
    if name in _COMMON or len(name) < 8:
        return False
    words = 1 + sum(1 for ch in name[1:] if ch.isupper())
    if words >= 3:
        return True
    head = name[:next((i for i, ch in enumerate(name) if ch.isupper()), len(name))]
    return words == 2 and len(name) >= 10 and head not in _GENERIC_HEADS


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
    prof.begin("swift_collect")
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

    swift_files = sorted(f for f in file_set if f.lower().endswith(".swift"))

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
            if not (d["method"] or "").startswith("ts-swift"):
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
            if not (e.extraction_method or "").startswith("ts-swift"):
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

    def _module_dir(mod: str) -> str | None:
        cands = sorted({f.rpartition("/")[0] for f in swift_files
                        if f.rpartition("/")[0] == mod or
                        f.rpartition("/")[0].endswith("/" + mod)})
        return cands[0] if len(cands) == 1 else None

    def _owner_label(node_id: str) -> str:
        n = _node(node_id)
        if n is None:
            return ""
        parts = (getattr(n, "qualified_name", "") or "").split(".")
        return parts[-2] if len(parts) >= 2 else ""

    _methods_memo: dict = {}

    def _methods_of(owner_label: str, member: str) -> list[str]:
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

    def _type_labels(label: str, dirs: set[str]) -> list[str]:
        out = []
        for f in swift_files:
            if _dir_of(f) not in dirs:
                continue
            for nid in (file_syms.get(f, {}) or {}).get(label, []):
                n = _node(nid)
                if n is not None and getattr(n, "type", "") in (
                        "class", "interface", "type"):
                    out.append(nid)
        return sorted(set(out))

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

    prof.end("swift_collect")
    prof.begin("swift_bind")
    mod_dirs: dict[tuple[str, str], str] = {}
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = a.get("specifier", "")
        if not spec:
            continue
        short = spec.split(".")[-1]
        if short in _STDLIB or spec in _STDLIB:
            diag["external_imports"] += 1
            continue
        dd = _module_dir(short)
        if dd is None:
            diag["unresolved_imports"] += 1
            continue
        diag["resolved_imports"] += 1
        mod_dirs[(key, short)] = dd
        anchor = sorted(f for f in swift_files
                        if f.rpartition("/")[0] == dd)
        if anchor:
            add_edge(key, mod_id(owner_rel(key)), "depends_on",
                     mod_id(anchor[0]), d, "resolved", "resolved", 0.9,
                     {"specifier": spec, "via": "swift-module"},
                     site=_sf("swiftdep", spec, dd))
            diag["depends_on_derived"] += 1
    prof.end("swift_bind")
    prof.begin("swift_calls")

    all_dirs = sorted({_dir_of(f) for f in swift_files})
    _subtree_memo: dict = {}

    def _subtree(root: str) -> set[str]:
        if root not in _subtree_memo:
            _subtree_memo[root] = {d for d in all_dirs
                                   if d == root or d.startswith(root + "/")}
        return _subtree_memo[root]

    def _scope_dirs(here: str) -> set[str]:
        """The file's module: a SwiftPM target is the whole
        `Sources/<M>/` (or `Tests/<M>/`) subtree; elsewhere its own dir."""
        parts = here.split("/")
        if len(parts) >= 3 and parts[0] in ("Sources", "Source", "Tests"):
            return _subtree("/".join(parts[:2]))
        return {_dir_of(here)}

    def _imported_dirs(key: str) -> set[str]:
        out: set[str] = set()
        for (k, _m), dd in mod_dirs.items():
            if k == key:
                out |= _subtree(dd)
        return out

    def _superclass_qns(method_nid: str) -> list[str]:
        """Superclass qualified names via the extractor's own inherits
        placeholders (no bound edges needed)."""
        mn = _node(method_nid)
        if mn is None:
            return []
        mqn = getattr(mn, "qualified_name", "") or ""
        owner_qn = mqn.rpartition(".")[0]
        mfile = getattr(mn, "file_path", "") or ""
        dirs = _scope_dirs(mfile) | {
            dd for (_k, _m), dd in mod_dirs.items()
            if _k.endswith(":" + mfile)}
        out = []
        for _key2, hd in heritage:
            hn = _node(hd["source"])
            if hn is None:
                continue
            if (getattr(hn, "qualified_name", "") or "") != owner_qn:
                continue
            ha = _attrs(hd)
            short = (ha.get("superclass") or "").split(".")[-1]
            if not short:
                continue
            for nid in _type_labels(short, dirs):
                n = _node(nid)
                if n is not None:
                    out.append(getattr(n, "qualified_name", "") or "")
        return sorted(set(o for o in out if o))

    _by_label: dict = {}
    for _f, _tbl in file_syms.items():
        for _lab, _ids in _tbl.items():
            for _nid in _ids:
                _n = _node(_nid)
                if _n is not None and getattr(_n, "type", "") == "method":
                    _by_label.setdefault(_lab, set()).add(_nid)

    _type_labs = {lab for _tbl in file_syms.values() for lab, ids in _tbl.items()
                  if any(getattr(_node(x), "type", "") in ("class", "interface", "type") for x in ids)}

    def _repo_rooted(a: dict) -> bool:
        """A field path from a typed root the binder can walk (a repo type
        or a result/callback marker): left to it."""
        frt = a.get("field_root_type") or ""
        return bool(a.get("field_path")) and (frt.startswith("()") or frt.split("<")[0] in _type_labs)

    def _methods_named(label: str) -> list[str]:
        return sorted(_by_label.get(label, ()))

    for key, d in unres_calls:
        a = _attrs(d)
        kind = a.get("callee_kind", "")
        here = owner_rel(key)
        tgt = None
        if kind == "function":
            base = a.get("base", "")
            owner_label = _owner_label(d["source"])
            cands = _methods_of(owner_label, base) if owner_label else []
            if not cands:
                # own module first; an imported module's top-level function
                # only when the own module has no declaration of that name
                for dirs, kinds in ((_scope_dirs(here), ("function", "method")),
                                    (_imported_dirs(key), ("function",))):
                    for f in swift_files:
                        if _dir_of(f) not in dirs:
                            continue
                        for nid in (file_syms.get(f, {}) or {}).get(base, []):
                            n = _node(nid)
                            if n is not None and getattr(n, "type", "") in kinds:
                                cands.append(nid)
                    if cands:
                        break
            if not cands and base[:1].islower():
                # `@_exported import` chains (BuiltInRules -> Core -> Base) are
                # not followed: the package's only top-level func of that name
                for f in swift_files:
                    for nid in (file_syms.get(f, {}) or {}).get(base, []):
                        n = _node(nid)
                        if n is not None and getattr(n, "type", "") == "function":
                            cands.append(nid)
            uniq = sorted(set(cands))
            tgt = uniq[0] if len(uniq) == 1 else None
            if tgt is None and len(uniq) > 1:
                diag["ambiguous_calls"] += 1
            if tgt is None and not uniq:
                for nid in _type_labels(base, _scope_dirs(here)):
                    tgt = nid
                    add_edge(key, d["source"], "instantiates", tgt, d,
                             "resolved", "resolved", 0.9,
                             {"via": "swift-resolve", "name": base},
                             site=_sf("swiftinst", base))
                    diag["resolved_heritage"] += 1
                    break
        elif kind in ("selector", "this", "super"):
            base = a.get("base", "")
            member = a.get("member", "")
            if kind in ("this", "super") or not base:
                if kind == "super":
                    cands = []
                    for qn in _superclass_qns(d["source"]):
                        cands.extend(
                            _methods_of(qn.split(".")[-1], member))
                else:
                    owner_label = _owner_label(d["source"])
                    cands = _methods_of(owner_label, member) \
                        if owner_label else []
                uniq = sorted(set(cands))
                tgt = uniq[0] if len(uniq) == 1 else None
                if tgt is None and len(uniq) > 1:
                    diag["ambiguous_calls"] += 1
            else:
                dd = mod_dirs.get((key, base))
                dirs = {dd} if dd else _scope_dirs(here)
                cands = []
                for nid in _type_labels(base, dirs):
                    n = _node(nid)
                    qn = (getattr(n, "qualified_name", "") or "")
                    cands.extend(_methods_of(qn.split(".")[-1], member))
                uniq = sorted(set(cands))
                tgt = uniq[0] if len(uniq) == 1 else None
                if tgt is None and len(uniq) > 1:
                    diag["ambiguous_calls"] += 1
        if tgt is None and kind in ("selector", "complex"):
            # `x.leadingTrivia.containsNewlines()` on an untyped receiver: a
            # distinctive name only one repo method/extension declares
            # (inferred, like Ruby's unique-name rule)
            member = a.get("member", "")
            base = a.get("base", "")
            if member and _distinctive(member) and not base[:1].isupper() and base != "self" \
                    and not any(a.get(k) for k in ("receiver_type", "receiver_owner",
                                                   "receiver_static", "chain_root_type")) \
                    and not _repo_rooted(a):
                ms = _methods_named(member)
                if len(ms) == 1 and ms[0] != d["source"]:
                    diag["resolved_calls"] += 1
                    add_edge(key, d["source"], "calls", ms[0], d, "inferred", "inferred", 0.6,
                             {"via": "swift-unique-name"},
                             site=_sf("swiftuniq", member))
            continue
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "swift-resolve"},
                     site=_sf("swiftcall", a.get("base", ""),
                              a.get("member", "")))
    for key, d in heritage + instantiates:
        a = _attrs(d)
        raw = a.get("superclass") or a.get("interface") or \
            a.get("created") or ""
        short = (raw or "").split(".")[-1].split("<")[0].strip()
        if not short:
            continue
        here = owner_rel(key)
        dd = mod_dirs.get((key, short))
        dirs = {dd} if dd else _scope_dirs(here)
        cands = _type_labels(short, dirs)
        # ...plus module-bound dirs for heritage names.
        if not cands:
            for (_k, _mod), _dd in mod_dirs.items():
                if _k == key:
                    cands.extend(_type_labels(short, {_dd}))
        uniq = sorted(set(cands))
        tgt = uniq[0] if len(uniq) == 1 else None
        if tgt and not tgt.startswith("ext:") and \
                not tgt.startswith("file:") and tgt != d["source"]:
            diag["resolved_heritage"] += 1
            add_edge(key, d["source"], d["relation"], tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "swift-resolve", "name": short},
                     site=_sf("swiftheritage", short))
    prof.end("swift_calls")
    prof.begin("swift_files")
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "swift")
    prof.end("swift_files")
    return [], extra, diag


def _emit_file_deps(extra, diag, store, new_results, mod_id, method,
                    version, tag):
    """File-level dependencies from resolved cross-file facts (same
    contract as go/java/rust/cpp/kotlin/lua). Deterministic."""
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
