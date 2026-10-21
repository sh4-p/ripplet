"""Service-stage Python import resolution (Phase 12 Priority 1).

Module-qualified binding — never a global label dictionary. ``imports``
edges resolve through package structure (absolute + relative), alias tables,
``__init__`` re-export chains (bounded, cycle-guarded), and explicit
``__all__`` gates. Anything else stays explicitly unresolved, external, or
ambiguous, with candidates preserved and reasons recorded.

Mirrors the ``js_resolve.resolve_repo`` shape (manifest-driven collection
from stored + new edges, per-owner derived edges, diagnostics) without
sharing its semantics: Python-specific rules live here. The structural
placeholder edges from extraction are never modified; resolution adds new
edges:

- resolved: ``imports`` module -> real symbol (site-fingerprinted id).
- ambiguous: one ``imports`` edge per candidate (``ambiguous`` + candidates).
- unresolved-internal: one marker ``imports`` edge to the same placeholder
  (site marks the verdict + reason).
- external (stdlib/installed/unknown top package): no new edge; the
  placeholder stands and the run diagnostics count it. This avoids an edge
  explosion on the common case while keeping externals distinguishable.
- star imports: file-level ``depends_on`` only, never per-symbol edges.
- ``depends_on`` file -> file is derived from verified (resolved) imports
  only, never from unresolved or fabricated targets.
"""
from __future__ import annotations
import sys

METHOD = "py-import-resolve"
VERSION = "2"  # v2: re-export chains resolve names the provider module defines itself (src/ layouts)
# Module-level edges for `from M import ...` statements live under their own
# method so per-symbol tallies (tests, benchmarks, and any consumer
# filtering on py-import-resolve) are undisturbed; purge/keep/drop logic in
# service.py treats both methods identically.
METHOD_MM = "py-module-import"
VERSION_MM = "1"
MAX_CHAIN = 3

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
EXTERNAL = "external"
UNRESOLVED = "unresolved"

_SYMBOL_TYPES = ("function", "method", "class", "test", "interface", "type", "enum",
                 "constant")


def _attrs(e) -> dict:
    a = e["attributes"] if isinstance(e, dict) else getattr(e, "attributes", {})
    if isinstance(a, dict):
        return a
    try:
        import json
        return json.loads(a or "{}")
    except Exception:
        return {}


def _prov(e) -> list:
    p = e["provenance"] if isinstance(e, dict) else getattr(e, "provenance_ids", [])
    if isinstance(p, list):
        return p
    try:
        import json
        return json.loads(p or "[]")
    except Exception:
        return []


def _stdlib() -> set[str]:
    std = getattr(sys, "stdlib_module_names", None)
    if std:
        return set(std)
    return {"os", "sys", "re", "json", "pathlib", "typing", "collections",
            "dataclasses", "ast", "io", "time", "math", "abc", "enum",
            "functools", "itertools", "threading", "subprocess", "unittest"}


_third_party_cache: dict[str, bool] = {}


def _installed(top: str) -> bool:
    """True when an import finder locates the top package WITHOUT importing
    it (``PathFinder.find_spec`` executes no code, unlike ``find_spec``)."""
    if top in _third_party_cache:
        return _third_party_cache[top]
    try:
        import importlib.machinery as _m
        ok = _m.PathFinder.find_spec(top) is not None
    except Exception:
        ok = False
    _third_party_cache[top] = ok
    return ok


def _package_of(mod_dotted: str, rel_path: str) -> str:
    """Owning package of the importing module (``__init__`` is its own package)."""
    if rel_path.replace("\\", "/").endswith("/__init__.py"):
        return mod_dotted
    return mod_dotted.rpartition(".")[0]


def _abs_module(importer_mod: str, importer_rel: str, level: int, from_mod: str) -> str:
    """Absolute dotted module for one import statement."""
    if level <= 0:
        return from_mod
    pkg = _package_of(importer_mod, importer_rel)
    parts = pkg.split(".") if pkg else []
    up = level - 1
    if up > len(parts):
        return ""  # beyond top-level: unresolvable, never guessed
    base = parts[:len(parts) - up]
    if from_mod:
        base = base + [from_mod]
    return ".".join(base)


def _add_edge(out: list, owner_key: str, source: str, relation: str, target: str,
              d: dict, base_attrs: dict, method: str, version: str,
              evidence: str, conf: str, score: float | None,
              site_parts: tuple, diag: dict, diag_key: str | None = None):
    from ...core.models import Edge
    from ...core.identifiers import canonical_edge_id, site_fingerprint
    attrs = dict(base_attrs)
    site = site_fingerprint(*site_parts)
    eid = canonical_edge_id(source, relation, target, d["file_path"], site)
    out.append((owner_key, Edge(
        eid, source, target, relation, evidence, conf, score,
        _prov(d), method, version, file_path=d["file_path"],
        start_line=d["start_line"], attributes=attrs)))
    if diag_key:
        diag[diag_key] = diag.get(diag_key, 0) + 1
    return eid


def resolve_repo(repo_id: str, file_set: set[str],
                 new_results: list, store, exclude_keys: set[str] | None = None,
                 fresh_keys: set[str] | None = None, prof=None) \
        -> tuple[list, list, dict]:
    """Returns (extra_nodes, [(owner_key, Edge)], diagnostics).

    ``new_results`` are ``(file_key, ExtractionResult)`` for freshly parsed
    files; stored manifests cover cached files. ``exclude_keys`` are ghost
    manifests (deleted or renamed-away) that never participate in binding.
    ``fresh_keys`` are re-parsed file keys whose stored rows still describe
    the PRE-edit state: their stored modules/symbols/imports are skipped
    (the fresh results above supersede them), while the files stay live.
    Only ``imports`` edges with ``python-ast`` method are considered; the
    structural placeholder edges themselves are left untouched.
    ``prof`` (optional StageProfiler) records ``py_collect`` / ``py_pass1``
    / ``py_pass2`` sub-stages; resolution semantics are unaffected.
    """
    exclude_keys = exclude_keys or set()
    fresh_rels = {k.split(":", 1)[-1] for k in (fresh_keys or set())}
    if prof is None:
        from ..evaluation.stageprofile import make_profiler as _mp
        prof = _mp(False)
    prof.begin("py_collect")
    diag = {"modules": 0, "resolved_imports": 0, "unresolved_imports": 0,
            "external_imports": 0, "ambiguous_imports": 0, "builtin_imports": 0,
            "chain_resolutions": 0, "depends_on_derived": 0, "star_imports": 0}

    # ---- module index: dotted module -> (module node id, rel path) ----
    modules: dict[str, tuple[str, str]] = {}
    dunder: dict[str, list[str]] = {}

    def _register_module(mod_dotted: str, node_id: str, rel: str, attrs: dict):
        if mod_dotted and mod_dotted not in modules:
            modules[mod_dotted] = (node_id, rel)
            if isinstance(attrs.get("dunder_all"), list):
                dunder[mod_dotted] = [str(x)[:120] for x in attrs["dunder_all"]][:500]

    for key, res in new_results:
        for n in res.nodes:
            if getattr(n, "type", "") == "module":
                _register_module(getattr(n, "qualified_name", "") or "", n.id,
                                 getattr(n, "file_path", ""),
                                 dict(getattr(n, "attributes", {}) or {}))
    try:
        rows = store.cx.execute(
            "SELECT id, qualified_name, file_path, attributes FROM nodes "
            "WHERE repository_id=? AND type='module'", (repo_id,)).fetchall()
    except Exception:
        rows = []
    for r in rows:
        if (r["file_path"] or "") in fresh_rels:
            continue  # superseded by this run's fresh parse
        try:
            import json as _j
            attrs = _j.loads(r["attributes"] or "{}")
        except Exception:
            attrs = {}
        _register_module(r["qualified_name"] or "", r["id"], r["file_path"] or "", attrs)
    ghosted = {k.split(":", 1)[-1] for k in exclude_keys}
    modules = {m: v for m, v in modules.items()
               if v[1] in file_set and v[1] not in ghosted}
    diag["modules"] = len(modules)

    # ---- nested package roots (Phase 14 P3) ----
    # A directory that contains packages (dirs with live `__init__.py`) but
    # is not itself a package is a syspath root by Python semantics: any
    # runtime with it on sys.path resolves its top-level packages from it.
    # Discovered purely from the live file set (no I/O, deterministic), these
    # roots supply ALIAS names (e.g. `pkg.api` for
    # `tests/fixtures/sample_py/pkg/api.py`) alongside canonical
    # workspace-root-relative names. The workspace root itself is always a
    # root; unrelated siblings never become roots unless they contain the
    # imported top-level package (and then only as candidates, see below).
    # A name resolving through two different roots (or root + nested) to
    # different files is AMBIGUOUS, never silently picked.
    def _discover_aliases():
        live = {r for r in file_set if r not in ghosted}
        norm = {}  # love posix form -> original rel
        for r in sorted(live):
            if r.endswith(".py"):
                norm[r.replace("\\", "/")] = r
        inits = {p for p in norm if p.rsplit("/", 1)[-1] == "__init__.py"}
        pkg_dirs = {p.rpartition("/")[0] for p in inits}
        by_rel = {}
        for m, (nid, rel) in modules.items():
            by_rel.setdefault(rel.replace("\\", "/"), []).append((m, nid))
        aliases: dict[str, list] = {}
        roots: set[str] = set()

        def _alias_for(nrel):
            d = nrel.rpartition("/")[0]
            # "" (workspace root itself a package) is never a nested root;
            # excluding it also guarantees termination.
            guard = nrel.count("/") + 2
            while d and d in pkg_dirs and guard > 0:
                d = d.rpartition("/")[0] if "/" in d else ""
                guard -= 1
            return d

        for nrel in sorted(norm):
            root = _alias_for(nrel)
            if not root:
                continue  # workspace-root-relative: canonical covers it
            roots.add(root)
            suffix = nrel[len(root) + 1:]
            if suffix == "__init__.py":
                continue  # root-level __init__ is not a module name
            if suffix.endswith("/__init__.py"):
                dotted = suffix[:-len("/__init__.py")].replace("/", ".")
            elif suffix.endswith(".py"):
                dotted = suffix[:-len(".py")].replace("/", ".")
            else:
                continue
            if not dotted:
                continue
            for _m, nid in by_rel.get(norm[nrel], []):
                entry = (nid, norm[nrel])
                if entry not in aliases.setdefault(dotted, []):
                    aliases[dotted].append(entry)
        to_canonical: dict[str, list] = {}
        for dotted, hits in aliases.items():
            for _nid, rel in hits:
                for cm, _mnid in by_rel.get(rel.replace("\\", "/"), []):
                    if cm not in to_canonical.setdefault(dotted, []):
                        to_canonical[dotted].append(cm)
        return aliases, roots, to_canonical

    module_aliases, nested_roots, alias_to_canonical = _discover_aliases()
    diag["nested_roots"] = len(nested_roots)
    diag["alias_modules"] = len(module_aliases)

    def _module_hits(dotted: str) -> list:
        """Canonical + alias hits for one dotted name, canonical first,
        deduplicated, deterministic. Distinct rels mean genuine ambiguity."""
        out = []
        if dotted in modules:
            out.append(modules[dotted])
        for entry in module_aliases.get(dotted, []):
            if entry not in out:
                out.append(entry)
        return out

    # ---- symbol + file index (real symbols only) ----
    symbols: dict[str, dict[str, list[str]]] = {}
    node_files: dict[str, str] = {}

    def _add_symbol(mod_dotted: str, name: str, nid: str, rel: str):
        if mod_dotted and name:
            node_files[nid] = rel
            symbols.setdefault(mod_dotted, {}).setdefault(name, [])
            if nid not in symbols[mod_dotted][name]:
                symbols[mod_dotted][name].append(nid)

    for key, res in new_results:
        for n in res.nodes:
            nt = getattr(n, "type", "")
            if nt not in _SYMBOL_TYPES:
                continue
            qn = getattr(n, "qualified_name", "") or ""
            mod, _, name = qn.rpartition(".")
            if "#" in name or not mod:
                continue  # overload discriminator: not directly importable
            _add_symbol(mod, getattr(n, "label", "") or name, n.id,
                        getattr(n, "file_path", ""))
    try:
        srows = store.cx.execute(
            "SELECT id, label, qualified_name, file_path FROM nodes "
            "WHERE repository_id=? AND type IN "
            "('function','method','class','test','interface','type','enum',"
            "'constant')",
            (repo_id,)).fetchall()
    except Exception:
        srows = []
    for r in srows:
        if (r["file_path"] or "") in fresh_rels:
            continue  # superseded by this run's fresh parse
        qn = r["qualified_name"] or ""
        mod, _, name = qn.rpartition(".")
        if "#" in name or not mod:
            continue
        _add_symbol(mod, r["label"] or name, r["id"], r["file_path"] or "")
    # Only symbols in live files participate (stale/deleted rows excluded).
    live_rels = {rel for rel in file_set if rel not in ghosted}
    symbols = {m: {n: [i for i in ids if node_files.get(i, "") in live_rels]
                   for n, ids in names.items()}
               for m, names in symbols.items()}
    symbols = {m: {n: ids for n, ids in names.items() if ids}
               for m, names in symbols.items() if m in modules}
    # Alias mirrors share the canonical entries (already live-filtered).
    # dunder lists are shared by reference (read-only downstream).
    for alias, cmods in alias_to_canonical.items():
        for cm in cmods:
            if cm in symbols:
                for name, ids in symbols[cm].items():
                    tgt = symbols.setdefault(alias, {}).setdefault(name, [])
                    for i in ids:
                        if i not in tgt:
                            tgt.append(i)
            if cm in dunder and alias not in dunder:
                dunder[alias] = dunder[cm]

    # ---- collect imports edges: stored manifests + new results ----
    imports: list[tuple[str, dict]] = []  # (owner_key, edge_row)
    try:
        repo_keys = sorted(k for k in store.list_file_keys()
                           if k.startswith(repo_id + ":") and k not in exclude_keys
                           and k.split(":", 1)[-1] not in fresh_rels)
    except Exception:
        repo_keys = []
    # Bulk-fetch manifest edges: identical (key, eid) iteration order and
    # identical row dicts as the old per-key + per-edge SELECT loops —
    # one LIKE scan per repo instead of one query per file, then the same
    # sorted-edge-ids-per-key walk. Read-only (Phase 15).
    eids_by_key: dict[str, list] = {}
    try:
        _rids = sorted({k.split(":", 1)[0] for k in repo_keys})
        _man: dict[str, set] = {}
        for _rid in _rids:
            # Join filters to python-imports edges up front: same (key, eid)
            # pairs the per-key walk would keep, without fetching every
            # manifest row and every edge row first (Phase 15, read-only).
            for _r in store.cx.execute(
                    "SELECT fe.file_key, fe.edge_id FROM file_edges fe "
                    "JOIN edges e ON e.id = fe.edge_id "
                    "WHERE fe.file_key LIKE ? AND e.relation='imports' "
                    "AND substr(e.method, 1, 6)='python'",
                    (_rid + ":%",)).fetchall():
                _man.setdefault(_r["file_key"], set()).add(_r["edge_id"])
        for key in repo_keys:
            if key in _man:
                eids_by_key[key] = sorted(_man[key])
    except Exception:
        # Fall back to per-key reads (same values, more round-trips).
        eids_by_key = {}
        for key in repo_keys:
            try:
                eids_by_key[key] = sorted(store.get_manifest_edges(key))
            except Exception:
                continue
    edge_rows: dict[str, dict] = {}
    _all_eids = sorted({eid for eids in eids_by_key.values() for eid in eids})
    for _i in range(0, len(_all_eids), 500):
        _chunk = _all_eids[_i:_i + 500]
        _q = ",".join("?" * len(_chunk))
        try:
            for _r in store.cx.execute(
                    f"SELECT * FROM edges WHERE id IN ({_q})", _chunk).fetchall():
                edge_rows[_r["id"]] = dict(_r)
        except Exception:
            # Chunk failure must not drop more than the old per-edge loop
            # would: retry those edges one by one.
            for _eid in _chunk:
                try:
                    _row = store.cx.execute(
                        "SELECT * FROM edges WHERE id=?", (_eid,)).fetchone()
                except Exception:
                    continue
                if _row is not None:
                    edge_rows[_eid] = dict(_row)
    for key in repo_keys:
        for eid in eids_by_key.get(key, []):
            row = edge_rows.get(eid)
            if row is None:
                continue
            d = dict(row)
            if d["relation"] == "imports" and (d["method"] or "").startswith("python"):
                imports.append((key, d))
    for key, res in new_results:
        for e in res.edges:
            if e.relation == "imports" and (e.extraction_method or "").startswith("python"):
                imports.append((key, {
                    "id": e.id, "source": e.source, "target": e.target,
                    "relation": e.relation, "method": e.extraction_method,
                    "provenance": list(e.provenance_ids or []),
                    "attributes": dict(e.attributes or {}),
                    "file_path": e.file_path, "start_line": e.start_line}))

    # ---- resolve each edge (deterministic id order) ----
    prof.end("py_collect")
    prof.begin("py_pass1")
    out: list[tuple[str, object]] = []
    deferred: list = []
    # Pass-1 direct resolutions, visible to pass-2 re-export chains below.
    # Strictly per-call (never global): no state leaks across runs or repos.
    # Values are (target_node_id, chain_path) tuples everywhere.
    resolved_direct: dict[tuple[str, str], tuple[str, list]] = {}
    # Module-level edges for `from M import ...` statements (Phase 24 Faz
    # A1): Python binds the named module alongside the symbol, so the
    # importer-module -> M-module edge is as real as the symbol edge.
    # One edge per (importer, module, statement line); the symbol edges
    # keep per-name lines. File-level depends_on is already derived from
    # the symbol binding, so none is added here (avoids duplicates).
    seen_mm: set = set()

    def _emit_mm(owner_key, d, base, target_mod):
        mh = _module_hits(target_mod)
        if len({rel for _nid, rel in mh}) != 1:
            return
        tgt = mh[0][0]
        key = (d["source"], tgt, d["start_line"])
        if key in seen_mm:
            return
        seen_mm.add(key)
        attrs = dict(base)
        # At module granularity what was imported is the module itself;
        # keeping the symbol-level `imported` string would double-count
        # this edge in per-symbol tallies.
        attrs["imported"] = target_mod
        _add_edge(out, owner_key, d["source"], "imports", tgt, d,
                  {**attrs, "resolution": "resolved", "via": "from-module"},
                  METHOD_MM, VERSION_MM, "resolved", "resolved", 0.9,
                  ("pymodimport", target_mod, tgt),
                  diag, "module_edges_derived")

    def _depends(owner_key, d, owner_rel, target_rel, base):
        if not target_rel or target_rel == owner_rel:
            return
        from ...core.identifiers import file_node_id
        src_fid = file_node_id(repo_id, owner_rel)
        dst_fid = file_node_id(repo_id, target_rel)
        _add_edge(out, owner_key, src_fid, "depends_on", dst_fid, d,
                  {"via_import": d["id"], "resolution": "resolved"},
                  METHOD, VERSION, "resolved", "resolved", 0.8,
                  ("depends", owner_rel, target_rel), diag, "depends_on_derived")

    def _target_file(nid: str) -> str:
        return node_files.get(nid, "")

    for owner_key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        owner_rel = owner_key.split(":", 1)[-1]
        importer_mod = None
        for m, (mid, _rel) in modules.items():
            if mid == d["source"]:
                importer_mod = m
                break
        if importer_mod is None:
            # Source module not live (deleted/stale manifest): leave as-is.
            diag["unresolved_imports"] += 1
            continue
        level = int(a.get("level", 0) or 0)
        from_mod = str(a.get("from_module", "") or "")
        names = list(a.get("names") or [])
        imported = str(a.get("imported", "") or "")
        base = {"resolution": "", "original_target": d["target"],
                "imported": imported, "alias": str(a.get("alias", "") or ""),
                "level": level, "from_module": from_mod}
        if level <= 0 and not from_mod:
            target_mod, name = imported, ""
        else:
            target_mod = _abs_module(importer_mod, owner_rel, level, from_mod)
            name = names[0] if names else ""
        if not target_mod:
            _add_edge(out, owner_key, d["source"], "imports", d["target"], d,
                      {**base, "resolution": "unresolved",
                       "reason": "beyond-top-level"},
                      METHOD, VERSION, "resolved", "unavailable", None,
                      ("unresolved", imported), diag, "unresolved_imports")
            continue
        top = target_mod.split(".")[0]
        hits = _module_hits(target_mod)
        if not hits:
            if top in _stdlib():
                diag["builtin_imports"] += 1
                base["reason"] = "stdlib:" + top
            elif _installed(top):
                diag["external_imports"] += 1
                base["reason"] = "installed:" + top
            elif any(m == top or m.startswith(top + ".")
                     for m in list(modules) + list(module_aliases)):
                _add_edge(out, owner_key, d["source"], "imports", d["target"], d,
                          {**base, "resolution": "unresolved",
                           "reason": "no-such-module:" + target_mod},
                          METHOD, VERSION, "resolved", "unavailable", None,
                          ("unresolved", imported), diag, "unresolved_imports")
            else:
                diag["external_imports"] += 1
                base["reason"] = "unknown-top-package:" + top
            continue
        if len({rel for _nid, rel in hits}) > 1:
            # Same dotted name via incompatible roots: every candidate is
            # reported, none silently picked.
            for tgt, _rel in sorted(hits):
                _add_edge(out, owner_key, d["source"], "imports", tgt, d,
                          {**base, "resolution": "ambiguous",
                           "candidates": sorted(t for t, _r in hits)},
                          METHOD, VERSION, "resolved", "hypothetical", 0.5,
                          ("pyimport-ambiguous-root", imported, tgt),
                          diag, None)
            diag["ambiguous_imports"] += 1
            continue
        if name == "*":
            # Star import: file-level dependency only, never per-symbol edges
            # (and no module edge either: the file-level depends_on above
            # already records the module linkage for star statements).
            diag["star_imports"] += 1
            _depends(owner_key, d, owner_rel, hits[0][1], base)
            continue
        if not name:
            tgt = hits[0][0]
            _add_edge(out, owner_key, d["source"], "imports", tgt, d,
                      {**base, "resolution": "resolved"},
                      METHOD, VERSION, "resolved", "resolved", 0.9,
                      ("pyimport", imported, tgt), diag, "resolved_imports")
            resolved_direct[(target_mod, "")] = (tgt, [])
            _depends(owner_key, d, owner_rel, hits[0][1], base)
            continue
        # `from . import x`: the submodule wins (import system binds it).
        if level > 0 and not from_mod:
            sub = target_mod + "." + name
            sub_hits = _module_hits(sub)
            if sub_hits and len({rel for _nid, rel in sub_hits}) == 1:
                tgt = sub_hits[0][0]
                _add_edge(out, owner_key, d["source"], "imports", tgt, d,
                          {**base, "resolution": "resolved",
                           "note": "submodule-binding"},
                          METHOD, VERSION, "resolved", "resolved", 0.9,
                          ("pyimport", imported, tgt), diag, "resolved_imports")
                _depends(owner_key, d, owner_rel, sub_hits[0][1], base)
                continue
            if sub_hits:
                for tgt, _rel in sorted(sub_hits):
                    _add_edge(out, owner_key, d["source"], "imports", tgt, d,
                              {**base, "resolution": "ambiguous",
                               "candidates": sorted(t for t, _r in sub_hits)},
                              METHOD, VERSION, "resolved", "hypothetical", 0.5,
                              ("pyimport-ambiguous-root", imported, tgt),
                              diag, None)
                diag["ambiguous_imports"] += 1
                continue
        cands = [nid for nid in symbols.get(target_mod, {}).get(name, [])
                 if _target_file(nid) in live_rels]
        if len(cands) == 1:
            tgt = cands[0]
            _add_edge(out, owner_key, d["source"], "imports", tgt, d,
                      {**base, "resolution": "resolved"},
                      METHOD, VERSION, "resolved", "resolved", 0.9,
                      ("pyimport", imported, tgt), diag, "resolved_imports")
            resolved_direct[(target_mod, name)] = (tgt, [])
            tfile = _target_file(tgt)
            if tfile:
                _depends(owner_key, d, owner_rel, tfile, base)
            if from_mod:
                _emit_mm(owner_key, d, base, target_mod)
            continue
        if len(cands) > 1:
            for tgt in sorted(cands):
                _add_edge(out, owner_key, d["source"], "imports", tgt, d,
                          {**base, "resolution": "ambiguous",
                           "candidates": sorted(cands)},
                          METHOD, VERSION, "resolved", "hypothetical", 0.5,
                          ("pyimport-ambiguous", imported, tgt),
                          diag, None)
            diag["ambiguous_imports"] += 1
            if from_mod:
                _emit_mm(owner_key, d, base, target_mod)
            continue
        dall = dunder.get(target_mod)
        if dall is not None and name not in dall:
            _add_edge(out, owner_key, d["source"], "imports", d["target"], d,
                      {**base, "resolution": "unresolved",
                       "reason": "not-exported(__all__)"},
                      METHOD, VERSION, "resolved", "unavailable", None,
                      ("unresolved", imported), diag, "unresolved_imports")
            if from_mod:
                _emit_mm(owner_key, d, base, target_mod)
            continue
        # Not defined directly: defer to the re-export chain pass below.
        deferred.append((owner_key, d, base, importer_mod, owner_rel,
                         target_mod, name, imported))
    # ---- pass 2: re-export chains to fixpoint (bounded, deterministic) ----
    # `from M import N` where M itself re-exports N: follow M's own imports
    # (named, with their pass-1 resolutions, or star-expansions gated by the
    # provider's public names). Distinct final targets across providers mean
    # ambiguity, never a guess.
    # Phase 15 (exact index): imports grouped once by owner file. Iterating
    # one owner's list in global encounter order reproduces the old
    # filter-then-dedupe sequence exactly, without scanning every file's
    # imports per deferred edge.
    _imports_by_owner: dict[str, list] = {}
    for _ok, _od in imports:
        _imports_by_owner.setdefault(_ok.split(":", 1)[-1], []).append((_ok, _od))

    def _providers(mod_dotted: str, mod_rel: str, want: str):
        provs = []  # (provider_abs_module, via_star)
        for _ok, od in _imports_by_owner.get(mod_rel, []):
            oa = _attrs(od)
            onames = list(oa.get("names") or [])
            if want in onames:
                px = _abs_module(mod_dotted, mod_rel,
                                 int(oa.get("level", 0) or 0),
                                 str(oa.get("from_module", "") or ""))
                if px:
                    provs.append((px, False))
            if "*" in onames:
                px = _abs_module(mod_dotted, mod_rel,
                                 int(oa.get("level", 0) or 0),
                                 str(oa.get("from_module", "") or ""))
                if px:
                    provs.append((px, True))
        # deterministic, deduplicated
        seen, uniq = set(), []
        for px, star in provs:
            if (px, star) not in seen:
                seen.add((px, star))
                uniq.append((px, star))
        return uniq

    def _chain_targets(mod_dotted: str, mod_rel: str, want: str, seen_mods: tuple):
        found: dict[str, list] = {}
        for px, star in _providers(mod_dotted, mod_rel, want):
            if px in seen_mods or not _module_hits(px):
                continue
            if star:
                dallx = dunder.get(px)
                if dallx is not None:
                    if want not in dallx:
                        continue
                elif want.startswith("_"):
                    continue
                for nid in symbols.get(px, {}).get(want, []):
                    if _target_file(nid) in live_rels:
                        found.setdefault(nid, []).append(px + ":*")
                continue
            key = (px, want)
            if key in resolved_direct:
                found.setdefault(resolved_direct[key][0], []).append(px)
            else:
                # the provider defines it itself (`utils.py: from ._internal import f`
                # re-exports `_internal.f`): resolve it here, not only through a
                # pass-1 entry, which another importer may have keyed differently
                # (`src.pkg._internal` vs `pkg._internal` in a src/ layout)
                for nid in symbols.get(px, {}).get(want, []):
                    if _target_file(nid) in live_rels:
                        found.setdefault(nid, []).append(px)
        return found

    pending = list(deferred)
    prof.end("py_pass1")
    prof.begin("py_pass2")
    for _round in range(MAX_CHAIN + 1):
        progressed = False
        still: list = []
        for rec in pending:
            owner_key, d, base, _im, owner_rel, target_mod, name, imported = rec
            _mh = _module_hits(target_mod)
            mod_rel = _mh[0][1] if _mh else ""
            found = _chain_targets(target_mod, mod_rel, name, (target_mod,))
            # one more hop when the provider itself re-exports (bounded)
            if not found:
                for px, _star in _providers(target_mod, mod_rel, name):
                    _pxh = _module_hits(px)
                    if _pxh and px != target_mod:
                        px_rel = _pxh[0][1]
                        sub = _chain_targets(px, px_rel, name, (target_mod, px))
                        for tgt, via in sub.items():
                            found.setdefault(tgt, []).extend([target_mod] + via)
            if len(found) == 1:
                tgt, via = next(iter(found.items()))
                diag["chain_resolutions"] += 1
                _add_edge(out, owner_key, d["source"], "imports", tgt, d,
                          {**base, "resolution": "resolved",
                           "via_chain": [target_mod] + list(via)},
                          METHOD, VERSION, "resolved", "resolved", 0.85,
                          ("pyimport", imported, tgt), diag, "resolved_imports")
                resolved_direct[(target_mod, name)] = (tgt, [target_mod] + list(via))
                tfile = _target_file(tgt)
                if tfile:
                    _depends(owner_key, d, owner_rel, tfile, base)
                if base.get("from_module"):
                    _emit_mm(owner_key, d, base, target_mod)
                progressed = True
            elif len(found) > 1:
                for tgt in sorted(found):
                    _add_edge(out, owner_key, d["source"], "imports", tgt, d,
                              {**base, "resolution": "ambiguous",
                               "candidates": sorted(found)},
                              METHOD, VERSION, "structural", "deterministic", 1.0,
                              ("pyimport-ambiguous", imported, tgt),
                              diag, None)
                diag["ambiguous_imports"] += 1
                if base.get("from_module"):
                    _emit_mm(owner_key, d, base, target_mod)
                progressed = True
            else:
                still.append(rec)
        pending = still
        if not progressed:
            break
    for rec in pending:
        owner_key, d, base, _im, _orel, _tm, name, imported = rec
        _add_edge(out, owner_key, d["source"], "imports", d["target"], d,
                  {**base, "resolution": "unresolved",
                   "reason": "no-such-symbol:" + name},
                  METHOD, VERSION, "resolved", "unavailable", None,
                  ("unresolved", imported), diag, "unresolved_imports")
        if base.get("from_module"):
            _emit_mm(owner_key, d, base, _tm)
    prof.end("py_pass2")
    return [], out, diag
