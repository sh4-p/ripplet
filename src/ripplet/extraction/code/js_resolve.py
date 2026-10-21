"""Service-stage JavaScript/TypeScript resolution (Phase 3).

Module-scoped binding — never a global label dictionary. Imports resolve through
specifiers (``module_resolution``), export maps (including ``export *`` chains),
and per-file alias tables; calls bind through those aliases, same-module
declarations, ``this``-in-class, and ``super``-via-heritage. Anything else stays
explicitly unresolved, with ambiguity preserved as candidate lists.
"""
from __future__ import annotations
import json
from . import module_resolution as mr
from .jsts import module_qname
from ...core.identifiers import file_node_id, canonical_edge_id

METHOD = "jsts-resolve"
VERSION = "8"  # v8: member calls never bind to a same-named import; v7: member calls through a default import (export default {f}, module.exports.f); v6: `from "."` / `from ".."` resolve to the directory index; v5: explicit `export {a} from` re-exports bind importers of the barrel; v4: named imports of `export * as` namespaces; v2: pure namespace imports (`import * as x`) bind their member calls


def _attrs(e) -> dict:
    a = e["attributes"] if isinstance(e, dict) else getattr(e, "attributes", {})
    if isinstance(a, dict):
        return a
    try:
        return json.loads(a or "{}")
    except Exception:
        return {}


def _prov(e) -> list:
    p = e["provenance"] if isinstance(e, dict) else getattr(e, "provenance_ids", [])
    if isinstance(p, list):
        return p
    try:
        return json.loads(p or "[]")
    except Exception:
        return []


def _eid(e) -> str:
    return e["id"] if isinstance(e, dict) else e.id


def resolve_repo(repo_id: str, repo_root: str, file_set: set[str],
                 new_results: list, store, exclude_keys: set[str] | None = None,
                 fresh_keys: set[str] | None = None, prof=None) \
        -> tuple[list, list, dict]:
    """Returns (extra_nodes, [(owner_key, Edge)], diagnostics) for one repo.

    ``exclude_keys`` are file manifests treated as ghosts (deleted or
    renamed-away files whose rows may still be visible pre-reconciliation):
    their exports, aliases, classes and nodes never participate in binding.
    ``fresh_keys`` are re-parsed file keys whose stored rows still describe
    the PRE-edit state: skipped in favor of ``new_results`` (the files stay
    live).
    ``prof`` (optional StageProfiler) records ``js_collect`` / ``js_bind``
    sub-stages; resolution semantics are unaffected.
    """
    exclude_keys = exclude_keys or set()
    fresh_rels = {k.split(":", 1)[-1] for k in (fresh_keys or set())}
    if prof is None:
        from ..evaluation.stageprofile import make_profiler as _mp
        prof = _mp(False)
    prof.begin("js_collect")
    from ...core.models import Edge, Node
    from ...core.models import Location
    diag = {"modules": 0, "resolved_imports": 0, "unresolved_imports": 0,
            "external_imports": 0, "builtin_imports": 0, "star_expansions": 0,
            "resolved_calls": 0, "ambiguous_calls": 0, "resolved_exports": 0,
            "unsupported_config": []}
    tscfg = mr.load_tsconfig(repo_root)
    diag["unsupported_config"] = list(tscfg.unsupported)
    pkg = mr.load_package(repo_root)
    if pkg.unsupported:
        diag["unsupported_config"] += list(pkg.unsupported)

    def mod_id(rel: str) -> str:
        return file_node_id(repo_id, rel + "#module")

    # ---- collect import/export/call records: new results + stored repo state
    imports: list[tuple[str, dict]] = []   # (owner_key, edge_row)
    exports: list[tuple[str, dict]] = []
    unres_calls: list[tuple[str, dict]] = []
    repo_keys = sorted(k for k in store.list_file_keys()
                       if k.startswith(repo_id + ":") and k not in exclude_keys
                       and k.split(":", 1)[-1] not in fresh_rels)
    for key in repo_keys:
        for eid in sorted(store.get_manifest_edges(key)):
            row = store.cx.execute("SELECT * FROM edges WHERE id=?", (eid,)).fetchone()
            if row is None:
                continue
            d = dict(row)
            if d["relation"] == "imports":
                imports.append((key, d))
            elif d["relation"] == "exports":
                exports.append((key, d))
            elif d["relation"] in ("calls_unresolved", "instantiates") and \
                    d["method"].startswith("jsts"):
                unres_calls.append((key, d))
    for key, res in new_results:
        for e in res.edges:
            d = {"id": e.id, "source": e.source, "target": e.target, "relation": e.relation,
                 "method": e.extraction_method, "provenance": list(e.provenance_ids or []),
                 "attributes": dict(e.attributes or {}), "file_path": e.file_path,
                 "start_line": e.start_line}
            if e.relation == "imports":
                imports.append((key, d))
            elif e.relation == "exports":
                exports.append((key, d))
            elif e.relation in ("calls_unresolved", "instantiates") and \
                    e.extraction_method.startswith("jsts"):
                unres_calls.append((key, d))

    # ---- module + export maps
    def owner_rel(key: str) -> str:
        return key.split(":", 1)[-1]
    export_map: dict[str, dict[str, str]] = {}   # module_rel -> exported_name -> symbol id
    for key, d in exports:
        a = _attrs(d)
        if a.get("re_export") or a.get("via_star"):
            continue  # derived edges are recomputed every run; only direct
            # exports seed the map (stale derivations must not pollute it)
        tgt = d["target"]
        if tgt.startswith("ext:"):
            continue
        export_map.setdefault(owner_rel(key), {})[a.get("exported_as", "")] = tgt
    # export * records (module_rel -> [(owner_key, specifier, alias)])
    stars: list[tuple[str, str, str, dict]] = []
    for key, d in imports:
        a = _attrs(d)
        if a.get("export_star") and a.get("specifier"):
            stars.append((owner_rel(key), key, a["specifier"], a))

    # `export * as util from "./util.js"` -> (module_rel, "util") -> specifier
    ns_reexports: dict[tuple[str, str], str] = {}
    for key, d in imports:
        a = _attrs(d)
        for alias in a.get("ns_exports", []) or []:
            ns_reexports[(owner_rel(key), alias)] = a.get("specifier", "")

    def spec_target(from_rel: str, spec: str):
        rr = mr.resolve_specifier(spec, from_rel, repo_root, file_set, tscfg, pkg)
        return rr

    def star_names(mod_rel: str, depth: int = 0, seen: frozenset = frozenset()) -> dict[str, list[str]]:
        """Transitive export-* expansion with cycle guard. name -> [symbol ids]."""
        if depth > 5 or mod_rel in seen:
            return {}
        seen = seen | {mod_rel}
        out: dict[str, list[str]] = {}
        for name, sid in export_map.get(mod_rel, {}).items():
            out.setdefault(name, []).append(sid)
        for (owner_rel_, _key, spec, _a) in stars:
            if owner_rel_ != mod_rel:
                continue
            rr = spec_target(mod_rel, spec)
            if rr.kind == "internal" and rr.target:
                for name, sids in star_names(rr.target, depth + 1, seen).items():
                    out.setdefault(name, []).extend(s for s in sids if s not in out.get(name, []))
        return out

    # explicit re-exports (`export {isX, y as z} from './guards.js'`) feed the
    # export map, so importers of a barrel bind through it; `export *` was
    # expanded by star_names, these were not (chains settle over a few passes)
    for _pass in range(4):
        grew = False
        for key, d in imports:
            a = _attrs(d)
            names = a.get("names") or []
            if not names or not a.get("specifier"):
                continue
            rr = spec_target(owner_rel(key), a["specifier"])
            if rr.kind != "internal" or not rr.target:
                continue
            src_map = dict(export_map.get(rr.target, {}))
            for name, sids in star_names(rr.target).items():
                if len(sids) == 1:
                    src_map.setdefault(name, sids[0])
            own = export_map.setdefault(owner_rel(key), {})
            for nm in names:
                if not isinstance(nm, dict):
                    continue
                tgt = src_map.get(nm.get("imported", ""))
                exp = nm.get("exported", "") or nm.get("imported", "")
                if tgt and not str(tgt).startswith("ext:") and exp not in own:
                    own[exp] = tgt
                    grew = True
        if not grew:
            break

    # ---- per-file alias tables (import bindings incl. stored)
    aliases: dict[str, dict[str, dict]] = {}
    for key, d in imports:
        a = _attrs(d)
        spec = a.get("specifier", "")
        b = a.get("bindings", {}) or {}
        tbl = aliases.setdefault(key, {})
        if isinstance(b, dict):
            if b.get("default"):
                tbl[b["default"]] = {"kind": "default", "specifier": spec,
                                     "type_only": a.get("type_only", False)}
            for nb in b.get("named", []) or []:
                if isinstance(nb, dict) and nb.get("local"):
                    tbl[nb["local"]] = {"kind": "named", "imported": nb.get("imported", ""),
                                        "specifier": spec,
                                        "type_only": a.get("type_only", False)}
            # `import * as util from "./util.js"` has no named bindings: the
            # namespace must be registered outside the loop above (it was
            # inside it, so a pure namespace import never bound anything)
            if b.get("namespace"):
                tbl[b["namespace"]] = {"kind": "namespace", "specifier": spec,
                                       "type_only": a.get("type_only", False)}
                # NOTE: namespaces resolve to modules, never to symbols — do NOT
                # put them in binding_target (a module id there would shadow a
                # same-name named binding and poison call resolution).
    # require() namespace bindings live in calls_unresolved attrs? No — they were
    # recorded as imports edges with namespace binding. Covered above.

    # module node per file for module-level edge endpoints (Phase 24 Faz
    # A2): exactly one module node per file is bound; files without one
    # (or with several, which never happens in practice) are skipped and
    # counted rather than guessed.
    module_by_file: dict[str, str] = {}
    for key in repo_keys:
        srel = owner_rel(key)
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is not None and n.type == "module" and \
                    (n.file_path or "") == srel and srel not in module_by_file:
                module_by_file[srel] = nid
    for key, res in new_results:
        srel = owner_rel(key)
        for n in getattr(res, "nodes", []):
            if n.type == "module" and (n.file_path or "") == srel \
                    and srel not in module_by_file:
                module_by_file[srel] = n.id
    # same-file type table for heritage upgrades (class/interface/type/enum),
    # built up-front so step 1b can use it.
    same_file_types: dict[str, dict[str, str]] = {}  # file rel -> {label: node id}
    for key in repo_keys:
        srel = owner_rel(key)
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is not None and n.type in ("class", "interface", "type", "enum") \
                    and (n.file_path or "") == srel:
                same_file_types.setdefault(srel, {}).setdefault(n.label, n.id)
    for key, res in new_results:
        srel = owner_rel(key)
        for n in getattr(res, "nodes", []):
            if n.type in ("class", "interface", "type", "enum") and (n.file_path or "") == srel:
                same_file_types.setdefault(srel, {}).setdefault(n.label, n.id)

    extra_nodes: list = []
    extra: list[tuple[str, object]] = []  # (owner_key, Edge)
    from ...core.models import Edge as EdgeCls

    def add_edge(owner_key: str, src: str, rel: str, tgt: str, src_row: dict,
                 etype: str, cclass: str, score, attrs: dict, site: str = ""):
        from ...core.identifiers import site_fingerprint as _sf
        try:
            occ = _attrs(src_row).get("occurrences")
            if occ and "occurrences" not in attrs:
                attrs = dict(attrs)
                attrs["occurrences"] = occ
        except Exception:
            pass
        e = EdgeCls(canonical_edge_id(src, rel, tgt, owner_rel(owner_key), site),
                    src, tgt, rel, etype, cclass, score, _prov(src_row),
                    METHOD, VERSION, file_path=owner_rel(owner_key),
                    start_line=src_row.get("start_line") or 0, attributes=attrs)
        extra.append((owner_key, e))
        return e

    def _emit_module_import(key: str, src_row: dict, owner: str, spec: str,
                            local: str, tgt, orig: str = "") -> None:
        """Module->symbol `imports` edge for one resolved ESM binding
        (Phase 24 Faz A2): the same binding the call resolver uses,
        surfaced at module granularity so module-framed queries
        (import dumps, neighbor scans) see it. Single real-symbol
        targets only: None (unresolved), ambiguous lists, and external
        placeholders emit nothing (no-guess rule). Files without
        exactly one module node are skipped and counted."""
        if not tgt or not isinstance(tgt, str) or tgt.startswith("ext:"):
            return
        mid = module_by_file.get(owner)
        if not mid:
            diag["module_node_missing"] = diag.get("module_node_missing", 0) + 1
            return
        from ...core.identifiers import site_fingerprint as _sfm
        diag["resolved_module_imports"] = \
            diag.get("resolved_module_imports", 0) + 1
        add_edge(key, mid, "imports", tgt, src_row,
                 "resolved", "resolved", 0.9,
                 {"via": "esm-binding", "imported": orig or local,
                  "local": local, "specifier": spec},
                 site=_sfm("esmimport", spec, local, tgt))

    def call_site(src_row: dict) -> str:
        """Stable per-call-site fingerprint so two call sites of one target keep
        distinct, order-independent edges (never line numbers)."""
        from ...core.identifiers import site_fingerprint as _sf
        a = _attrs(src_row)
        bind = a.get("import_binding") or {}
        return _sf(a.get("callee_kind", ""), a.get("base", ""), a.get("member", ""),
                   bind.get("specifier", "") if isinstance(bind, dict) else "",
                   bind.get("imported", "") if isinstance(bind, dict) else "",
                   a.get("namespace_member", ""), str(a.get("arity", "")),
                   a.get("via", ""))

    # ---- 1. import specifiers -> depends_on + binding targets
    prof.end("js_collect")
    prof.begin("js_bind")
    explicit_reexports: set[tuple[str, str]] = set()  # (module_rel, exported_as)
    binding_target: dict[tuple[str, str], str | None] = {}  # (owner_key, local) -> symbol|None
    ambiguous: dict[tuple[str, str], list] = {}
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = a.get("specifier", "")
        if not spec:
            continue
        rr = spec_target(owner_rel(key), spec)
        if rr.kind == "internal" and rr.target:
            diag["resolved_imports"] += 1
            add_edge(key, mod_id(owner_rel(key)), "depends_on", mod_id(rr.target), d,
                     "resolved", "resolved", 0.9,
                     {"specifier": spec, "via": rr.via,
                      "dynamic": a.get("dynamic", False),
                      "export_star": a.get("export_star", False),
                      "re_export": a.get("re_export", False)})
            if a.get("export_star"):
                # Module-level re-export edge (Phase 35): `export * from`
                # re-exports the whole module namespace, not just the
                # symbols star_names() expands below. The module edge is
                # as real as the symbol ones (A1 doctrine) and is what
                # module-framed import dumps match. Guarded like
                # _emit_module_import: both module nodes must exist.
                from ...core.identifiers import site_fingerprint as _sfm2
                src_mid = module_by_file.get(owner_rel(key))
                tgt_mid = module_by_file.get(rr.target)
                if src_mid and tgt_mid:
                    diag["star_module_exports"] = \
                        diag.get("star_module_exports", 0) + 1
                    add_edge(key, src_mid, "exports", tgt_mid, d,
                             "documented", "claimed", None,
                             {"via_star": spec, "re_export": True},
                             site=_sfm2("star-module", spec, rr.target))
            tmap = dict(export_map.get(rr.target, {}))
            for name, sids in star_names(rr.target).items():
                tmap.setdefault(name, sids[0] if len(sids) == 1 else None)
                if len(sids) > 1:
                    ambiguous[(key, name)] = sids
            b = a.get("bindings", {}) or {}
            if isinstance(b, dict):
                if b.get("default"):
                    tgt = tmap.get("default")
                    binding_target[(key, b["default"])] = tgt
                    _emit_module_import(key, d, owner_rel(key), spec,
                                        b["default"], tgt)
                for nb in b.get("named", []) or []:
                    if isinstance(nb, dict) and nb.get("local"):
                        tgt = tmap.get(nb.get("imported", ""))
                        if tgt is None and b.get("namespace") == nb["local"] and \
                                nb.get("imported") not in tmap:
                            # single-name require(): `module.exports = X` is the value
                            tgt = tmap.get("default")
                        if tgt is None and nb.get("imported") in tmap and \
                                (key, nb["imported"]) in ambiguous:
                            ambiguous[(key, nb["local"])] = ambiguous[(key, nb["imported"])]
                        binding_target[(key, nb["local"])] = tgt
                        _emit_module_import(key, d, owner_rel(key), spec,
                                            nb.get("local", ""), tgt,
                                            orig=nb.get("imported", ""))
                # NOTE: namespaces are intentionally absent from binding_target:
                # namespace members resolve via _namespace_member_target at call
                # time. Storing the module id here would shadow a same-name named
                # binding (e.g. destructured require) and poison call resolution.
            # re-export names (explicit re-exports win over later star
            # expansion for the same exported name — see step 2)
            for nm in a.get("names", []) or []:
                if not isinstance(nm, dict):
                    continue
                tgt = tmap.get(nm.get("imported", ""))
                if tgt:
                    from ...core.identifiers import site_fingerprint as _sf
                    diag["resolved_exports"] += 1
                    add_edge(key, mod_id(owner_rel(key)), "exports", tgt, d,
                             "documented", "claimed", None,
                             {"exported_as": nm.get("exported", ""), "re_export": True,
                              "from": spec},
                             site=_sf("re-export", spec, nm.get("imported", ""),
                                       nm.get("exported", "")))
                    explicit_reexports.add((owner_rel(key), nm.get("exported", "")))
        elif rr.kind == "external":
            diag["external_imports"] += 1
        elif rr.kind == "builtin":
            diag["builtin_imports"] += 1
        else:
            diag["unresolved_imports"] += 1

    # ---- 1b. same-file heritage upgrade (class/interface/type/enum only)
    heritage_candidates: list[tuple[str, dict]] = []
    for key, res in new_results:
        for e in getattr(res, "edges", []):
            if e.relation in ("inherits", "implements"):
                heritage_candidates.append((key, {
                    "id": e.id, "source": e.source, "target": e.target,
                    "relation": e.relation, "provenance": list(e.provenance_ids or []),
                    "attributes": dict(e.attributes or {}),
                    "file_path": e.file_path, "start_line": e.start_line}))
    for key in repo_keys:
        for eid in store.get_manifest_edges(key):
            row = store.cx.execute("SELECT * FROM edges WHERE id=?", (eid,)).fetchone()
            if row is None:
                continue
            d = dict(row)
            if d["relation"] in ("inherits", "implements"):
                heritage_candidates.append((key, d))
    for key, d in heritage_candidates:
        if not str(d["target"]).startswith("ext:"):
            continue
        a = _attrs(d)
        raw = a.get("superclass") or a.get("interface") or ""
        short = raw.split(".")[-1].split("<")[0].strip()
        if not short:
            continue
        tgt = (same_file_types.get(owner_rel(key), {}) or {}).get(short)
        via = "same-file"
        if (tgt is None or tgt == d["source"]) and short in (aliases.get(key, {}) or {}):
            # Heritage through an import (e.g. `implements Shape` with
            # `import type { Shape } from './types'`): same binding table as calls.
            tgt = binding_target.get((key, short))
            via = "import"
        if tgt and not tgt.startswith("ext:") and not tgt.startswith("file:") \
                and tgt != d["source"]:
            from ...core.identifiers import site_fingerprint as _sf2
            diag["resolved_heritage"] = diag.get("resolved_heritage", 0) + 1
            add_edge(key, d["source"], d["relation"], tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": via, "name": short},
                     site=_sf2("heritage", short))

    # ---- 2. export * expansion -> exports edges
    for (mod_rel, key, spec, _a) in stars:
        rr = spec_target(mod_rel, spec)
        if rr.kind == "internal" and rr.target:
            names = star_names(rr.target)
            if names:
                diag["star_expansions"] += 1
            for name, sids in sorted(names.items()):
                if name == "default":
                    continue  # export * never re-exports default
                if (mod_rel, name) in explicit_reexports:
                    continue  # explicit `export {x as name}` wins over the star
                from ...core.identifiers import site_fingerprint as _sf
                if len(sids) == 1:
                    add_edge(key, mod_id(mod_rel), "exports", sids[0], {"id": "",
                                 "provenance": [], "start_line": 0},
                             "documented", "claimed", None,
                             {"exported_as": name, "via_star": spec},
                             site=_sf("star", spec, name))
                else:
                    for sid in sids:
                        add_edge(key, mod_id(mod_rel), "exports", sid, {"id": "",
                                     "provenance": [], "start_line": 0},
                                 "documented", "claimed", None,
                                 {"exported_as": name, "via_star": spec,
                                  "ambiguous": True, "candidates": sids},
                                 site=_sf("star", spec, name, sid))

    # ---- class/method tables for this./super (new + stored defines of repo)
    class_methods: dict[str, dict[str, str]] = {}  # class qname -> {method: id}
    class_id_by_qname: dict[str, str] = {}         # class qname -> node id
    for key in repo_keys:
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is None:
                continue
            if n.type == "method" and n.qualified_name:
                cls, _, _m = n.qualified_name.rpartition(".")
                if cls:
                    class_methods.setdefault(cls, {})[n.label] = n.id
            elif n.type == "class" and n.qualified_name:
                class_id_by_qname.setdefault(n.qualified_name, n.id)
    # (new results are not yet stored; add them directly when available)
    for _key, res in new_results:
        for n in getattr(res, "nodes", []):
            if n.type == "method" and n.qualified_name:
                cls, _, _m = n.qualified_name.rpartition(".")
                if cls:
                    class_methods.setdefault(cls, {}).setdefault(n.label, n.id)
            elif n.type == "class" and n.qualified_name:
                class_id_by_qname.setdefault(n.qualified_name, n.id)
    inherit_name: dict[str, str] = {}  # class node id -> superclass name
    for key in repo_keys:
        for eid in store.get_manifest_edges(key):
            row = store.cx.execute("SELECT * FROM edges WHERE id=?", (eid,)).fetchone()
            if row is None:
                continue
            d = dict(row)
            if d["relation"] == "inherits":
                a = _attrs(d)
                if a.get("superclass"):
                    inherit_name[d["source"]] = a["superclass"]
    for _key, res in new_results:
        for e in getattr(res, "edges", []):
            if e.relation == "inherits" and (e.attributes or {}).get("superclass"):
                inherit_name.setdefault(e.source, e.attributes["superclass"])

    def node_by_id(nid: str):
        for _key, res in new_results:
            for n in getattr(res, "nodes", []):
                if n.id == nid:
                    return n
        n = store.get_node(nid)
        if n is not None:
            owners = [r["file_key"] for r in store.cx.execute(
                "SELECT file_key FROM file_nodes WHERE node_id=?", (nid,)).fetchall()]
            if owners and all(k in exclude_keys for k in owners):
                return None  # ghost of an excluded file
        return n

    # ---- 3. call binding
    for key, d in sorted(unres_calls, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        if d["relation"] == "instantiates":
            # constructor via binding or same-module class table
            tgt = binding_target.get((key, a.get("member", "")))
            if tgt is None and a.get("namespace_member"):
                base0 = (a.get("base", "") or "").split(".")[0]
                # namespace -> module: look up member in its export map
                tgt = _namespace_member_target(key, base0, a.get("namespace_member", ""),
                                               aliases, export_map, star_names,
                                               owner_rel, spec_target, repo_root,
                                               file_set, tscfg, pkg, ns_reexports, stars)
            if tgt and not tgt.startswith("ext:") and not tgt.startswith("file:"):
                diag["resolved_calls"] += 1
                add_edge(key, d["source"], "instantiates", tgt, d,
                         "resolved", "resolved", 0.85,
                         {"via": "import", "arity": a.get("arity", 0)},
                         site=call_site(d))
            continue
        member = a.get("member", "")
        base = a.get("base", "")
        if a.get("scoped_receiver") == "super":
            owner = node_by_id(d["source"])
            qual = getattr(owner, "qualified_name", "") if owner else ""
            enclosing = next((cq for cq in class_methods
                              if qual == cq or qual.startswith(cq + ".")), "")
            parent = inherit_name.get(class_id_by_qname.get(enclosing, ""), "") if enclosing else ""
            if parent:
                # parent may be a simple name; match repo classes by short name
                cands = [cq for cq in class_id_by_qname
                         if cq.split(".")[-1] == parent.split(".")[-1]]
                if len(cands) == 1:
                    tgt = class_methods.get(cands[0], {}).get(
                        "constructor" if not member else member)
                    if tgt:
                        diag["resolved_calls"] += 1
                        add_edge(key, d["source"], "calls", tgt, d,
                                 "resolved", "resolved", 0.7, {"via": "super"},
                                 site=call_site(d))
                        continue
            continue  # unresolved super stays explicit
        if a.get("scoped_receiver") == "this" and member:
            owner = node_by_id(d["source"])
            qual = getattr(owner, "qualified_name", "") if owner else ""
            for cq, meths in class_methods.items():
                if qual == cq or qual.startswith(cq + "."):
                    if member in meths and d["source"] != meths[member]:
                        diag["resolved_calls"] += 1
                        add_edge(key, d["source"], "calls", meths[member], d,
                                 "resolved", "resolved", 0.9, {"via": "this"},
                                 site=call_site(d))
                        break
            continue
        # import binding / namespace member. Only a bare `html(..)` is the
        # imported `html`; `c.html(..)` is a member of whatever `c` is
        bound = binding_target.get((key, member)) if not base else None
        if bound is not None and not bound.startswith("ext:") and not bound.startswith("file:"):
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", bound, d,
                     "resolved", "resolved", 0.85,
                     {"via": "import", "arity": a.get("arity", 0)},
                     site=call_site(d))
            continue
        if a.get("namespace_member"):
            base0 = (base or "").split(".")[0]
            tgt = _namespace_member_target(key, base0, a["namespace_member"], aliases,
                                           export_map, star_names, owner_rel, spec_target,
                                           repo_root, file_set, tscfg, pkg, ns_reexports, stars)
            if tgt:
                diag["resolved_calls"] += 1
                add_edge(key, d["source"], "calls", tgt, d,
                         "resolved", "resolved", 0.85,
                         {"via": "namespace", "arity": a.get("arity", 0)},
                         site=call_site(d))
                continue
            continue
        if (key, member) in ambiguous:
            diag["ambiguous_calls"] += 1
            d2 = dict(d)
            extra_amb = list(ambiguous[(key, member)])
            # record candidates on a copy marker via diagnostics only (no edge)
            diag.setdefault("ambiguous_details", []).append(
                {"call": d["id"], "candidates": extra_amb})
            continue
        # plain same-name across modules: deliberately NOT resolved (§7)

    diag["modules"] = len({owner_rel(k) for k in repo_keys})
    prof.end("js_bind")
    return extra_nodes, extra, diag


def _namespace_member_target(key, base0, member, aliases, export_map, star_names,
                             owner_rel, spec_target, repo_root, file_set, tscfg, pkg,
                             ns_reexports=None, stars=()):
    ns_reexports = ns_reexports or {}
    b = (aliases.get(key, {}) or {}).get(base0)
    if not b or b.get("kind") not in ("namespace", "named", "default"):
        return None
    rr = spec_target(owner_rel(key), b.get("specifier", ""))
    if rr.kind != "internal" or not rr.target:
        return None
    if b["kind"] == "default":
        # `import utils from './utils'` + `utils.f()`: `export default { f }`
        # (member `default.f`) or CommonJS `module.exports.f = f` / `= { f }`
        dmap = dict(export_map.get(rr.target, {}))
        tgt = dmap.get("default." + member) or dmap.get(member)
        return tgt if tgt and not str(tgt).startswith("ext:") else None
    if b["kind"] == "named":
        # `import { util } from "./core"` where core does `export * as util from`
        mod, name = rr.target, b.get("imported", "")
        for _hop in range(4):
            spec = ns_reexports.get((mod, name))
            if spec is not None:
                break
            # the namespace may itself be re-exported through `export *` barrels
            nxt = [s for (o, _k, s, _a) in stars if o == mod]
            hit = None
            for s in nxt:
                r2 = spec_target(mod, s)
                if r2.kind == "internal" and r2.target and                         any(k[0] == r2.target for k in ns_reexports):
                    hit = r2.target
                    break
            if hit is None:
                return None
            mod = hit
        else:
            return None
        rr = spec_target(mod, spec)
        if rr.kind != "internal" or not rr.target:
            return None
    tmap = dict(export_map.get(rr.target, {}))
    for name, sids in star_names(rr.target).items():
        tmap.setdefault(name, sids[0] if len(sids) == 1 else None)
    tgt = tmap.get(member)
    if tgt and not tgt.startswith("ext:"):
        return tgt
    return None
