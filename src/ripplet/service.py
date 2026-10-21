from __future__ import annotations
import hashlib
import time
from pathlib import Path
from .config import RippletConfig
from .discovery.scanner import scan_workspace
from .discovery.workspace import Workspace
from .extraction.pipeline import extract_file, EXTRACTOR_VERSIONS
from .extraction.structured.schema_code import SCHEMA_CODE_VERSION as _SCHEMA_CODE_VERSION
from .extraction.structured.schema_code import is_schema_path as _is_schema_path
from .extraction.pipeline import UNIV_TS_EXTRACTORS
from .extraction.incremental import is_cache_valid
from .extraction.validation import validate_result
from .graph.builder import merge_results
from .graph.resolver import resolve_unresolved
from .storage.sqlite_store import SQLiteStore
from .storage.lock import IndexLock, LockError, lock_path_for
from .core.models import Edge, Node
from .core.errors import IncompleteScanError
from .core.identifiers import canonical_edge_id

STATUS = {"state": "ready"}

# Resolution pipeline version for the no-op decision (Priority 3): bump when
# any resolver's semantics change (name-resolver "2", jsts-resolve "1",
# py-import-resolve "1", go-resolve "1", extractor versions in
# EXTRACTOR_VERSIONS, js/univ config state). A bump forces full
# re-resolution even when file caches hit. The marker is persisted in meta
# after every full run and compared below.
# v2 (Phase 35): lexical `..` collapse in module_resolution, dual-shape
# class_heritage in jsts, module-level star-re-export edges in js_resolve.
# v3 (Phase 38 Wave 0): go-resolve semantics (new derived method).
# v4 (Phase 39 Wave 1): rust/java-resolve semantics (new derived methods).
# v5 (Phase 40 Wave 2): csharp/php-resolve semantics (new derived methods).
# v6 (Phase 41 Wave 3): cpp/c/kotlin/lua-resolve semantics (new derived
# methods; C++ declaration-vs-definition preference, Kotlin companion +
# same-package binding, Lua require-alias binding).
# v7 (Phase 42 Wave 4): swift/dart/gdscript-resolve semantics (new
# derived methods; Swift module-suffix binding, Dart package/part
# binding, GDScript class_name/preload binding).
# v8 (precision pass): receiver-aware Python binding — name-resolver no
# longer guesses Python calls by unique label; py-call-bind binds via
# resolved imports / module members / cross-file heritage; go-resolve v2
# binds receiver-typed selector calls; superseded_edges hides
# calls_unresolved rows a resolved edge covers.
# v9 (precision pass, all languages): overloads split by arity/argument
# types, receiver typing via fields/locals/params/chains/statics,
# inheritance-aware member lookup, lexical constructor resolution,
# java-resolve v2 (no package-wide bare-call binding).
RESOLUTION_VERSION = "9"
RESOLUTION_MARKER_KEY = "resolution_marker"


def _resolution_marker(chash: str, js_state: dict[str, str],
                       univ_state: dict[str, str] | None = None) -> str:
    parts = [RESOLUTION_VERSION, _derived_code_hash(), chash] + \
        [f"{rid}={js_state.get(rid, '')}" for rid in sorted(js_state)] + \
        [f"u:{rid}={(univ_state or {}).get(rid, '')}"
         for rid in sorted(univ_state or {})]
    return "|".join(parts)


_DERIVED_HASH: list = []


def _derived_code_hash() -> str:
    """Fingerprint of the code that derives edges after extraction (binders,
    linkers, resolvers). An upgraded Ripplet must not keep serving edges
    its previous version derived just because no source file changed."""
    if not _DERIVED_HASH:
        import hashlib as _h
        here = Path(__file__).parent
        files = sorted(list((here / "graph").glob("*.py")) + list((here / "extraction" / "code").glob("*_resolve.py"))
                       + [here / "extraction" / "code" / "typed_params.py"])
        h = _h.sha256()
        for f in files:
            try:
                h.update(f.read_bytes())
            except OSError:
                pass
        _DERIVED_HASH.append(h.hexdigest()[:12])
    return _DERIVED_HASH[0]


def _stored_resolution_marker(store) -> str | None:
    try:
        row = store.cx.execute("SELECT v FROM meta WHERE k=?",
                               (RESOLUTION_MARKER_KEY,)).fetchone()
        return row["v"] if row else None
    except Exception:
        return None


def _ext_name_for(rel: str, language_overrides=None) -> str:
    """Extractor cache key (override-aware; delegates to pipeline so the
    lookup and the actual dispatch can never disagree)."""
    try:
        from .extraction.pipeline import ext_name_for as _ef
        return _ef(rel, language_overrides)
    except Exception:
        s = Path(rel).suffix.lower()
        if s == ".py":
            return "python-ast"
        return "generic-text"


def index_workspace(cfg: RippletConfig, progress=None, profile: bool = False) -> dict:
    """See `_index_workspace`. A successful run also stamps the index as
    caught up with the files (mcp/freshness.py reads it)."""
    out = _index_workspace(cfg, progress, profile)
    try:
        from .mcp.freshness import mark_fresh
        mark_fresh(cfg)
    except Exception:
        pass
    return out


def _index_workspace(cfg: RippletConfig, progress=None, profile: bool = False) -> dict:
    """Ownership-aware incremental index with rename handling and safe publication.

    - File lock prevents concurrent writers from silently overwriting each other.
    - Per-file manifests (file_nodes/file_edges) record exactly which facts each
      extraction produced; deletion removes only unsupported facts.
    - Rename candidates (same repo + same content hash, 1:1) become `renames_to`
      edges with the old file node kept as a tombstone (history preserved).
    - Resolution re-runs globally so cached files pick up new dependency targets.
    - One transaction publishes graph + manifests + snapshot + generation marker;
      failures roll back and keep the last valid graph.
    - `profile=True` (opt-in, benchmark tooling only) adds a machine-readable
      `stage_profile` to the summary; default output is unchanged.
    """
    global STATUS
    from .evaluation.stageprofile import make_profiler as _make_profiler
    _prof = _make_profiler(profile)
    _prof.begin("scope_lock")
    if not cfg.workspace_roots:
        raise ValueError("no workspace_roots configured; run 'ripplet init <dir>'")
    from .workspace_scope import check_scope, record_scope, WorkspaceMismatchError
    from .workspace_scope import missing_roots as _missing_roots
    from pathlib import Path as _P
    # Priority 0: bind (roots, storage) to the database identity BEFORE any
    # lock or write. Read-only probe; raises WorkspaceMismatchError instead
    # of silently mixing workspaces. A run whose roots are gone cannot write
    # anything, so it skips the gate: the historical incomplete-scan path
    # below raises IncompleteScanError with failed-generation bookkeeping.
    _gone = _missing_roots(cfg.workspace_roots)
    if _gone:
        scope = {"roots": [str(_P(r).resolve()) for r in cfg.workspace_roots],
                 "storage": str(_P(cfg.storage_path).resolve()),
                 "record_identity": False, "notice": ""}
    else:
        try:
            scope = check_scope(roots=cfg.workspace_roots,
                                storage_path=cfg.storage_path, intent="index")
        except WorkspaceMismatchError as e:
            STATUS = {"state": "failed", "error": str(e).splitlines()[0]}
            raise
    cfg.workspace_roots = list(scope["roots"])
    cfg.storage_path = scope["storage"]
    lock = IndexLock(lock_path_for(cfg.storage_path),
                     timeout_s=getattr(cfg, "lock_timeout_s", 30.0))
    try:
        lock.acquire()
    except LockError as e:
        STATUS = {"state": "failed", "error": str(e)}
        raise
    # Re-validate under the lock: a concurrent first-time init of the same
    # database from another workspace must not slip through check-then-act.
    # (Skipped when roots are gone: the incomplete-scan path owns that run.)
    if not _gone:
        try:
            scope = check_scope(roots=cfg.workspace_roots,
                                storage_path=cfg.storage_path, intent="index")
        except WorkspaceMismatchError as e:
            try:
                lock.release()
            except Exception:
                pass
            STATUS = {"state": "failed", "error": str(e).splitlines()[0]}
            raise
    _prof.end("scope_lock")
    _prof.begin("open_migrate")
    store = SQLiteStore(cfg.storage_path)
    t0 = time.time()
    try:
        # v1 (line-based identities) -> v2 (canonical): the extractor version
        # bumps force a reparse and the orphan sweep below retires superseded
        # IDs. The marker records that migration ran for this database.
        ver = store.cx.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        if ver is None or ver["v"] != "2":
            store.cx.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version','2')")
            store.cx.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('migrated_from',?)",
                             (ver["v"] if ver else "none",))
            store.commit()
    except Exception:
        pass
    errors: list[str] = []
    warnings_total = 0
    renames: list[dict] = []
    ambiguous_renames: list[dict] = []
    removed_nodes = 0
    removed_edges = 0
    # Declared-but-unknown override values are reported once per run and
    # otherwise ignored (a typo keeps defaults everywhere).
    try:
        from .config import normalize_language_overrides as _nlo
        _raw_ov = getattr(cfg, "language_overrides", None) or {}
        _norm_ov = _nlo(_raw_ov)
        _dropped = sorted(str(k) for k in _raw_ov
                          if isinstance(k, str) and
                          (k.strip().lower() if k.strip().lower().startswith(".")
                           else "." + k.strip().lower()) not in _norm_ov)
        if _dropped:
            errors.append(f"language_overrides ignored (unknown language): "
                          f"{', '.join(_dropped)[:200]}")
    except Exception:
        pass
    try:
        _prof.end("open_migrate")
        store.set_generation("indexing")
        chash = cfg.config_hash()
        # ---- 1. scan (completeness-guarded) ----
        _prof.begin("scan")
        scanned: dict[str, object] = {}  # file_key -> FileRecord
        scan_ok_repos: set[str] = set()
        repo_roots: dict[str, str] = {}
        for root in cfg.workspace_roots:
            ws_root = str(Path(root).resolve())
            if not Path(ws_root).exists():
                raise IncompleteScanError(f"workspace root inaccessible: {ws_root}")
            repo_id = "repo-" + hashlib.sha256(ws_root.encode()).hexdigest()[:10]
            repo_roots[repo_id] = ws_root
            try:
                files = scan_workspace(ws_root, repo_id, cfg.max_file_bytes)
            except OSError as e:
                raise IncompleteScanError(f"scan failed for {ws_root}: {e}")
            scan_ok_repos.add(repo_id)
            for fr in files:
                scanned[fr.repo_id + ":" + fr.rel_path] = fr
        current_keys = set(scanned.keys())
        stored_keys = store.list_file_keys()
        # Only reconcile repos whose scan completed.
        reconcilable = {k for k in stored_keys
                        if k.split(":", 1)[0] in scan_ok_repos or ":" not in k}
        gone_keys = set(reconcilable - current_keys)
        new_keys = set(current_keys - stored_keys)
        _prof.end("scan")
        _prof.set("files_scanned", len(scanned))
        _prof.set("repos", len(scan_ok_repos))

        # ---- 2. parse new/changed ----
        _prof.begin("parse")
        parsed_results: list[tuple[str, object]] = []  # (file_key, ExtractionResult)
        reused = 0
        total_files = len(scanned)
        # JS/TS resolution depends on tsconfig/package.json/grammar versions too;
        # fold that state into the cache key for jsts files only (no schema change).
        # Universal-grammar files (Phase 38) get the same treatment with their
        # own state (grammar wheels + engine version); other files keep the
        # plain config hash so toolchain changes never invalidate them.
        js_state: dict[str, str] = {}
        univ_state: dict[str, str] = {}
        for rid, rroot in repo_roots.items():
            try:
                js_state[rid] = _js_config_state(rroot)
            except Exception:
                js_state[rid] = "cfgjs:unavailable"
            try:
                univ_state[rid] = _univ_config_state(rroot)
            except Exception:
                univ_state[rid] = "cfguniv:unavailable"
        for key in sorted(current_keys):
            fr = scanned[key]
            ext = _ext_name_for(fr.rel_path, getattr(cfg, "language_overrides", None))
            ever = EXTRACTOR_VERSIONS.get(ext, "1")
            if _is_schema_path(fr.rel_path):
                ever = f"{ever}+sc{_SCHEMA_CODE_VERSION}"
            if ext == "jsts":
                effective_chash = chash + "|" + js_state.get(fr.repo_id, "")
            elif ext in UNIV_TS_EXTRACTORS:
                effective_chash = chash + "|" + univ_state.get(fr.repo_id, "")
            else:
                effective_chash = chash
            stored = store.get_file(key)
            if stored and is_cache_valid(stored, fr.sha256, ever, effective_chash):
                reused += 1
                continue
            try:
                res = extract_file(fr.abs_path, fr.rel_path, fr.repo_id, cfg.project_id,
                                   getattr(cfg, "language_overrides", None))
                verrs = validate_result(res)
                if verrs:
                    res.warnings.extend(verrs[:5])
                warnings_total += len(res.warnings)
                parsed_results.append((key, res))
                store.set_file(key, fr.repo_id, fr.sha256, ext, ever, effective_chash)
                if progress:
                    try:
                        progress(fr.rel_path)
                    except Exception:
                        # A display problem (e.g. a non-ASCII file name on a
                        # cp1254 pipe) is never an extraction failure.
                        pass
            except Exception as e:
                errors.append(f"{fr.rel_path}: {e}")
                # No manifest update: a failed file keeps its last valid facts.
        _prof.end("parse")
        _prof.set("files_parsed", len(parsed_results))
        _prof.set("files_reused", reused)
        _prof.set("files_failed", len(errors))

        # ---- 3. rename detection (same repo + same content hash, 1:1) ----
        _prof.begin("rename_detect")
        sha_of = {k: scanned[k].sha256 for k in new_keys}
        gone_by_sha: dict[tuple, list[str]] = {}
        for k in gone_keys:
            row = store.get_file(k)
            if row:
                gone_by_sha.setdefault((row["repo_id"], row["sha256"]), []).append(k)
        new_by_sha: dict[tuple, list[str]] = {}
        for k in new_keys:
            fr = scanned[k]
            new_by_sha.setdefault((fr.repo_id, fr.sha256), []).append(k)
        renamed_old: set[str] = set()
        renamed_new: set[str] = set()
        for key, olds in gone_by_sha.items():
            news = new_by_sha.get(key, [])
            if len(olds) == 1 and len(news) == 1:
                old_k, new_k = olds[0], news[0]
                renamed_old.add(old_k)
                renamed_new.add(new_k)
                renames.append({"from": old_k.split(":", 1)[-1], "to": new_k.split(":", 1)[-1]})
            elif olds and news:
                ambiguous_renames.append(
                    {"sha": key[1][:19], "candidates_from": sorted(olds),
                     "candidates_to": sorted(news),
                     "note": "ambiguous content match; no merge performed, history preserved"})
        delete_keys = gone_keys - renamed_old
        _prof.end("rename_detect")
        _prof.set("renames", len(renames))
        _prof.set("ambiguous_renames", len(ambiguous_renames))

        # ---- 3b. reconcile true deletions BEFORE resolution so resolvers
        # never see ghost modules (incremental/full equivalence depends on it).
        # Tombstones stay later (they need manifests written first).
        _prof.begin("delete_reconcile")
        for key in sorted(delete_keys):
            row = store.get_file(key)
            rel = key.split(":", 1)[-1] if ":" in key else key
            nodes = store.get_manifest_nodes(key)
            edges = store.get_manifest_edges(key)
            rn, re_ = _remove_manifest_facts(store, key, nodes, edges)
            removed_nodes += rn
            removed_edges += re_
            store.cx.execute("DELETE FROM file_nodes WHERE file_key=?", (key,))
            store.cx.execute("DELETE FROM file_edges WHERE file_key=?", (key,))
            store.cx.execute("DELETE FROM files WHERE path=?", (key,))
            store.delete_evidence_by_source(rel)
            _sweep_dangling_provenance(store)
        # Renamed-away keys are ghosts for resolution purposes as well.
        resolve_exclude = set(delete_keys) | set(renamed_old)
        _prof.end("delete_reconcile")
        _prof.set("deleted_files", len(delete_keys))
        _prof.set("deleted_nodes", removed_nodes)
        _prof.set("deleted_edges", removed_edges)

        # ---- 3c. no-op fast path (Priority 3) ----
        _prof.begin("noop_check")
        # Derived edges are pure functions of (stored symbols, resolution
        # marker): when no file was parsed, deleted, or renamed AND the
        # marker matches the last full run, re-derivation reproduces
        # byte-identical edges, so the purge/resolve/publish cycle is
        # skipped. The run still records its generation marker, snapshot,
        # and (on first bind) workspace identity — a no-op commit, not a
        # skipped run. Ambiguous renames perform no merge, so they do not
        # block the fast path.
        from .extraction.code import tree_sitter as _ts_noop
        _marker = _resolution_marker(chash, js_state, univ_state)
        _touched = bool(parsed_results or delete_keys or renamed_old or renamed_new)
        if not _touched and _stored_resolution_marker(store) == _marker:
            n, e = store.count()
            store.cx.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
                             (hashlib.sha256(f"noop{time.time()}".encode()).hexdigest()[:12],
                              hashlib.sha256(str(time.time()).encode()).hexdigest()[:12],
                              f"index {total_files} files (no-op)", time.time(), n, e))
            if scope.get("record_identity"):
                try:
                    record_scope(store, scope["roots"])
                except Exception as exc:
                    errors.append(f"workspace identity not recorded: {exc}")
            store.set_generation("ready")
            store.commit()
            _prof.end("noop_check")
            dt = round(time.time() - t0, 2)
            summary = {"files": total_files, "reused": reused, "parsed": 0,
                       "nodes": n, "edges": e, "warnings": warnings_total,
                       "workspace": list(scope["roots"]),
                       "workspace_notice": scope.get("notice", ""),
                       "nodes_removed": 0, "edges_removed": 0,
                       "renames": [], "ambiguous_renames": ambiguous_renames,
                       "errors": errors[:20], "seconds": dt, "noop": True,
                       "resolution_marker": _marker,
                       "jsts_deep_parsed": 0, "jsts_fallback_files": 0,
                       "jsts_partial_files": 0,
                       "jsts_resolution": {}, "py_resolution": {},
                       "capabilities": _ts_noop.capabilities()}
            if profile:
                summary["stage_profile"] = _prof.report()
            store.close()
            lock.release()
            return summary

        # ---- 4. merge + global re-resolution ----
        _prof.end("noop_check")
        _prof.begin("purge")
        # Fresh parses supersede stored manifests: a re-parsed file's stored
        # rows still describe the PRE-edit state (manifests are rewritten at
        # publication), so resolution inputs must prefer new results over
        # stored rows for those keys. Otherwise a removed import/call would
        # be re-derived from its own stale manifest row and never withdrawn.
        parsed_keys = {k for k, _ in parsed_results}
        # Phase 14 (scoped invalidation): py/js resolvers are repo-scoped
        # derivations (they read only their repo's manifests/nodes), so repos
        # with no parsed/deleted/renamed files keep their stored edges when
        # the resolution marker matches (same pipeline + config state).
        # name-resolver stays global (cross-repo label matches possible).
        touched_repos: set[str] = {k.split(":", 1)[0] for k, _ in parsed_results}
        touched_repos |= {k.split(":", 1)[0] for k in delete_keys}
        touched_repos |= {k.split(":", 1)[0] for k in renamed_old | renamed_new}
        _marker_ok = (_stored_resolution_marker(store) == _marker)
        _keep_repos = ((set(repo_roots) - touched_repos) if _marker_ok else set())
        # Phase 14 (file-type scoping inside a touched repo): js derivation
        # consumes only js-family manifests (`imports`/`exports`/
        # jsts-method `calls_unresolved` + same-repo class table) plus repo
        # config, which the marker already covers. Python files contribute
        # none of those (verified: no `exports` relation and no
        # specifier/binding attrs in the python extractor; method
        # namespacing excludes its edges; rel paths carry extensions).
        # Hence js re-resolution can be skipped repo-by-repo when no
        # js-family file was parsed, deleted, or renamed there. The reverse
        # does not hold (the py module table includes all module nodes), so
        # py keeps the stricter repo rule above.
        from .extraction.code import tree_sitter as _ts_scope
        _touched_keys = ([k for k, _ in parsed_results] + sorted(delete_keys) +
                         sorted(renamed_old | renamed_new))
        _js_touched_repos: set[str] = set()
        for _k in _touched_keys:
            _sfx = Path(_k.split(":", 1)[-1]).suffix.lower()
            if _ts_scope.language_for_extension(_sfx) is not None:
                _js_touched_repos.add(_k.split(":", 1)[0])
        # Derived edges (name-resolver, jsts-resolve) are pure functions of the
        # current symbol tables: purge stored ones first so re-derivation never
        # accumulates stale-version duplicates (e.g. pre-site-fingerprint IDs).
        _js_keep_repos = ({r for r in set(repo_roots) if r not in _js_touched_repos}
                            if _marker_ok else set())
        # Phase 38 (universal grammars): go derivation consumes only
        # univ-family manifests (`imports` with specifiers, ts-go
        # `calls_unresolved`, same-repo package tables) plus repo config
        # (go.mod), which the marker already covers. Skip rule mirrors js.
        _univ_touched_repos: set[str] = set()
        for _k in _touched_keys:
            _sfx = Path(_k.split(":", 1)[-1]).suffix.lower()
            if _ts_scope.univ_language_for_extension(_sfx) is not None:
                _univ_touched_repos.add(_k.split(":", 1)[0])
        _univ_keep_repos = ({r for r in set(repo_roots)
                             if r not in _univ_touched_repos}
                            if _marker_ok else set())
        _purge_derived_edges(store, keep_repos=_keep_repos or None,
                             keep_js_repos=_js_keep_repos or None,
                             keep_univ_repos=_univ_keep_repos or None)
        _prof.end("purge")
        _prof.begin("name_resolve")
        _prof.begin("name_gather")
        merged = merge_results([r for _, r in parsed_results])
        node_owner: dict[str, str] = {}
        for key, res in parsed_results:
            for n in res.nodes:
                node_owner.setdefault(n.id, key)
        by_label: dict[str, list[str]] = {}
        node_types: dict[str, str] = {}
        for n in merged.nodes:
            by_label.setdefault(n.label, []).append(n.id)
            node_types[n.id] = n.type
        # Owner maps, prefetched once: resolution inputs below must prefer
        # fresh parses over stored rows for re-parsed files (see parsed_keys
        # above). Same values as the per-item manifest queries, set-based.
        _edge_owners: dict[str, list[str]] = {}
        for _r in store.cx.execute("SELECT edge_id, file_key FROM file_edges").fetchall():
            _edge_owners.setdefault(_r["edge_id"], []).append(_r["file_key"])
        _node_owners: dict[str, list[str]] = {}
        for _r in store.cx.execute("SELECT node_id, file_key FROM file_nodes").fetchall():
            _node_owners.setdefault(_r["node_id"], []).append(_r["file_key"])

        def _ghosted(item_id: str, owners: list[str]) -> bool:
            return bool(owners) and all(k in resolve_exclude for k in owners)

        def _superseded(owners: list[str]) -> bool:
            return bool(owners) and all(k in parsed_keys for k in owners)

        # Phase 15 (read-only): column-pruned scans instead of full
        # materialization. Same (id, label, type) triples in the same rowid
        # order as all_nodes(), without per-node JSON decoding; same
        # calls_unresolved dicts as the all_edges() filter below, in one
        # pass. Values and order are identical, round-trips are not. The
        # 100000 caps mirror the store helpers' own limits, so results are
        # identical on every measured corpus (largest: 96k edges).
        for _nr in store.cx.execute(
                "SELECT id, label, type FROM nodes LIMIT 100000").fetchall():
            _nid = _nr["id"]
            if _nid in node_types:
                continue
            _ow = _node_owners.get(_nid, [])
            if not _ghosted(_nid, _ow) and not _superseded(_ow):
                by_label.setdefault(_nr["label"], []).append(_nid)
                node_types[_nid] = _nr["type"]
        # unresolved edges: new + previously stored (dependency changes may resolve them now)
        _ue_rows = store.cx.execute(
            "SELECT * FROM edges WHERE relation='calls_unresolved' "
            "LIMIT 100000").fetchall()
        stored_unresolved = []
        for _er in _ue_rows:
            _ed = dict(_er)
            if not _ghosted(_ed["id"], _edge_owners.get(_ed["id"], [])) \
                    and not _superseded(_edge_owners.get(_ed["id"], [])):
                stored_unresolved.append(_ed)
        proxy_edges = list(merged.edges)
        for se in stored_unresolved:
            proxy_edges.append(_dict_edge(se))
        _prof.end("name_gather")
        _prof.begin("name_match")
        extra = resolve_unresolved(by_label, proxy_edges, node_types)
        _prof.end("name_match")
        _prof.begin("name_attribute")
        # attribute produced edges to the owning file of their unresolved source
        edge_owner: dict[str, str] = {}
        unresolved_owner: dict[str, str] = {}
        for key, res in parsed_results:
            for e in res.edges:
                if e.relation == "calls_unresolved":
                    unresolved_owner[e.id] = key
        for se in stored_unresolved:
            if se["id"] not in unresolved_owner:
                owners = _edge_owners.get(se["id"], [])
                if owners:
                    unresolved_owner[se["id"]] = owners[0]
        have_edge_ids = {e.id for e in merged.edges}
        # Phase 15 (read-only): id-only scan instead of a second full
        # all_edges() materialization. Same id set, no row decoding.
        have_edge_ids |= {r["id"] for r in
                          store.cx.execute(
                              "SELECT id FROM edges LIMIT 100000").fetchall()}
        new_resolved = []
        for e in extra:
            if e.id in have_edge_ids:
                continue
            have_edge_ids.add(e.id)
            new_resolved.append(e)
            # owner = owner of the unresolved edge it was derived from
            edge_owner[e.id] = "unknown"
        # map produced edges back via (source, target): find matching unresolved owner
        _attr_stats: dict = {}
        _attribute_resolved_owners(new_resolved, unresolved_owner, proxy_edges,
                                   node_owner, edge_owner, stats=_attr_stats)
        _prof.end("name_attribute")
        _prof.set("attr_index_size", _attr_stats.get("index_size", 0))
        _prof.set("attr_examinations", _attr_stats.get("owner_examinations", 0))
        _prof.set("attr_resolved", _attr_stats.get("resolved", 0))
        _prof.set("by_label_size", len(by_label))
        _prof.set("stored_unresolved", len(stored_unresolved))
        _prof.set("new_resolved", len(new_resolved))
        merged.edges.extend(new_resolved)
        _prof.end("name_resolve")

        # ---- 4a. Python import resolution (module-qualified, evidence-aware)
        _prof.begin("py_resolve")
        from .extraction.code import py_resolve as pyr
        py_diag: dict[str, dict] = {}
        py_new_by_repo: dict[str, list] = {}
        for key, res in parsed_results:
            if getattr(res, "extractor", "") == "python-ast":
                rid = key.split(":", 1)[0]
                py_new_by_repo.setdefault(rid, []).append((key, res))
        py_repos = set(py_new_by_repo)
        for row in store.cx.execute("SELECT path FROM files").fetchall():
            p = row["path"]
            if p.split(":", 1)[0] in repo_roots and \
                    Path(p.split(":", 1)[-1]).suffix.lower() == ".py":
                py_repos.add(p.split(":", 1)[0])
        py_edge_owners: dict[str, list] = {}
        if py_repos:
            for rid in sorted(py_repos):
                if rid not in scan_ok_repos:
                    continue
                if rid in _keep_repos:
                    # Untouched repo + compatible marker: stored py edges are
                    # still valid (resolver reads only this repo's state).
                    py_diag[rid] = {"skipped": True,
                                    "reason": "untouched-repo-marker-match"}
                    continue
                fset = {k.split(":", 1)[-1] for k in current_keys if k.startswith(rid + ":")}
                try:
                    _pnodes, pedges, pdiag = pyr.resolve_repo(
                        rid, fset, py_new_by_repo.get(rid, []), store,
                        exclude_keys=resolve_exclude,
                        fresh_keys={k for k, _ in py_new_by_repo.get(rid, [])},
                        prof=_prof)
                except Exception as e:
                    errors.append(f"py-resolve {rid}: {e}")
                    continue
                py_diag[rid] = pdiag
                for owner_key, e in pedges:
                    # Like JS edges: resolution is a deterministic function of
                    # current state, so upserting recomputes changed mappings
                    # instead of preserving stale targets.
                    have_edge_ids.add(e.id)
                    merged.edges.append(e)
                    edge_owner[e.id] = owner_key
                    # Manifest ownership: without this the orphan sweep deletes
                    # every derived edge at publish time.
                    if e.id not in py_edge_owners.setdefault(owner_key, []):
                        py_edge_owners[owner_key].append(e.id)

        # ---- 4b. JavaScript/TypeScript module-scoped resolution ----
        _prof.end("py_resolve")
        for _rid, _pd in py_diag.items():
            for _k in ("resolved_imports", "unresolved_imports", "ambiguous_imports",
                       "external_imports", "builtin_imports", "depends_on_derived"):
                if isinstance(_pd, dict) and isinstance(_pd.get(_k), int):
                    _prof.count("py_" + _k, _pd[_k])
            if isinstance(_pd, dict) and _pd.get("skipped"):
                _prof.count("py_repos_skipped")
            else:
                _prof.count("py_repos_resolved")
        _prof.begin("js_resolve")
        from .extraction.code import tree_sitter as ts_adapter
        js_diag: dict[str, dict] = {}
        js_new_by_repo: dict[str, list] = {}
        for key, res in parsed_results:
            if getattr(res, "extractor", "").startswith("jsts-"):
                rid = key.split(":", 1)[0]
                js_new_by_repo.setdefault(rid, []).append((key, res))
        jsts_repos = set(js_new_by_repo)
        for row in store.cx.execute("SELECT path FROM files").fetchall():
            p = row["path"]
            if p.split(":", 1)[0] in repo_roots and \
                    ts_adapter.language_for_extension(Path(p.split(":", 1)[-1]).suffix) is not None:
                jsts_repos.add(p.split(":", 1)[0])
        js_edge_owners: dict[str, str] = {}
        if jsts_repos:
            from .extraction.code import js_resolve as jsr
            for rid in sorted(jsts_repos):
                rroot = repo_roots.get(rid)
                if rroot is None or rid not in scan_ok_repos:
                    continue
                if rid in _keep_repos or (_marker_ok and rid not in _js_touched_repos):
                    # Untouched repo, or no js-family change in this repo, +
                    # compatible marker (which folds in this repo's js config
                    # state): stored js edges still valid.
                    js_diag[rid] = {"skipped": True,
                                    "reason": "untouched-repo-marker-match"
                                    if rid in _keep_repos else
                                    "no-js-change-marker-match"}
                    continue
                fset = {k.split(":", 1)[-1] for k in current_keys if k.startswith(rid + ":")}
                try:
                    _jnodes, jedges, jdiag = jsr.resolve_repo(
                        rid, rroot, fset, js_new_by_repo.get(rid, []), store,
                        exclude_keys=resolve_exclude,
                        fresh_keys={k for k, _ in js_new_by_repo.get(rid, [])},
                        prof=_prof)
                except Exception as e:
                    errors.append(f"js-resolve {rid}: {e}")
                    continue
                js_diag[rid] = jdiag
                for owner_key, e in jedges:
                    # JS edges always refresh: resolution is a deterministic
                    # function of current state, so upserting recomputes changed
                    # mappings instead of preserving stale targets.
                    have_edge_ids.add(e.id)
                    merged.edges.append(e)
                    edge_owner[e.id] = owner_key
                    # Manifest ownership: without this the orphan sweep deletes
                    # every derived edge at publish time.
                    if e.id not in js_edge_owners.setdefault(owner_key, []):
                        js_edge_owners[owner_key].append(e.id)

        # ---- 4c. Universal-grammar module-scoped resolution ----
        # One pass per deep univ language. Discovery is language-agnostic
        # (any univ file); each resolver filters its own extractor method,
        # so running all on a mixed repo is correct.
        _prof.end("js_resolve")
        from .extraction.code import tree_sitter as _tsa_univ
        univ_diags: dict[str, dict[str, dict]] = {}
        univ_new: dict[str, dict[str, list]] = {}
        univ_owners: dict[str, dict[str, list]] = {}
        for _ulang, _umod in (("go", "go_resolve"), ("rust", "rust_resolve"),
                              ("java", "java_resolve"),
                              ("c_sharp", "csharp_resolve"),
                              ("php", "php_resolve"),
                              ("cpp", "cpp_resolve"), ("c", "cpp_resolve"),
                              ("kotlin", "kotlin_resolve"),
                              ("lua", "lua_resolve"),
                              ("swift", "swift_resolve"),
                              ("dart", "dart_resolve"),
                              ("gdscript", "gdscript_resolve"),
                              ("ruby", "ruby_resolve")):
            _prof.begin(f"{_ulang}_resolve")
            _ext = f"ts-{_ulang}"
            _diag: dict[str, dict] = {}
            _new_by_repo: dict[str, list] = {}
            for key, res in parsed_results:
                if getattr(res, "extractor", "") == _ext:
                    rid = key.split(":", 1)[0]
                    _new_by_repo.setdefault(rid, []).append((key, res))
            _repos = set(_new_by_repo)
            for row in store.cx.execute("SELECT path FROM files").fetchall():
                p = row["path"]
                if p.split(":", 1)[0] in repo_roots and \
                        _tsa_univ.univ_language_for_extension(
                            Path(p.split(":", 1)[-1]).suffix) == _ulang:
                    _repos.add(p.split(":", 1)[0])
            _owners: dict[str, list] = {}
            if _repos:
                import importlib as _il
                try:
                    _rmod = _il.import_module(
                        f"ripplet.extraction.code.{_umod}")
                except Exception as e:
                    errors.append(f"{_ulang}-resolve import: {e}")
                    _prof.end(f"{_ulang}_resolve")
                    continue
                for rid in sorted(_repos):
                    rroot = repo_roots.get(rid)
                    if rroot is None or rid not in scan_ok_repos:
                        continue
                    if rid in _keep_repos or (_marker_ok and
                                             rid not in _univ_touched_repos):
                        _diag[rid] = {"skipped": True,
                                      "reason": "untouched-repo-marker-match"
                                      if rid in _keep_repos else
                                      f"no-{_ulang}-change-marker-match"}
                        continue
                    fset = {k.split(":", 1)[-1] for k in current_keys
                            if k.startswith(rid + ":")}
                    _peer = {}
                    if _ulang == "java":
                        # JVM interop: Java static-imports Kotlin top-level
                        # functions; fresh Kotlin results are not stored yet.
                        _peer["peer_results"] = [
                            (k, r) for k, r in parsed_results
                            if getattr(r, "extractor", "") == "ts-kotlin"
                            and k.startswith(rid + ":")]
                    try:
                        _unodes, _uedges, _udiag = _rmod.resolve_repo(
                            rid, rroot, fset, _new_by_repo.get(rid, []),
                            store, exclude_keys=resolve_exclude,
                            fresh_keys={k for k, _ in
                                        _new_by_repo.get(rid, [])},
                            prof=_prof, **_peer)
                    except Exception as e:
                        errors.append(f"{_ulang}-resolve {rid}: {e}")
                        continue
                    _diag[rid] = _udiag
                    for owner_key, e in _uedges:
                        have_edge_ids.add(e.id)
                        merged.edges.append(e)
                        edge_owner[e.id] = owner_key
                        if e.id not in _owners.setdefault(owner_key, []):
                            _owners[owner_key].append(e.id)
            univ_diags[_ulang] = _diag
            univ_new[_ulang] = _new_by_repo
            univ_owners[_ulang] = _owners
            _prof.end(f"{_ulang}_resolve")

        # ---- 5. publish: upserts + manifests ----
        from .graph.builder import union_duplicate_occurrences
        union_duplicate_occurrences(merged.edges)
        _prof.begin("publish_upserts")
        _prof.set("merged_nodes", len(merged.nodes))
        _prof.set("merged_edges", len(merged.edges))
        _prof.set("merged_evidence", len(merged.evidence))
        try:
            _nn, _ne = store.count()
            _nev = store.cx.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"]
            _fresh_db = (_nn == 0 and _ne == 0 and _nev == 0)
        except Exception:
            _fresh_db = False
        _prof.set("fresh_db_bulk", 1 if _fresh_db else 0)
        if _fresh_db:
            # Cold index into empty tables: bulk path provably equals the
            # per-row upserts (no prior provenance, first_seen, or FTS rows).
            store.bulk_insert_fresh(merged.nodes, merged.edges, merged.evidence)
        else:
            store.upsert_evidence(merged.evidence)
            store.upsert_nodes(merged.nodes)
            store.upsert_edges(merged.edges)
        _prof.end("publish_upserts")
        _prof.begin("manifests")
        for _rid, _jd in js_diag.items():
            if isinstance(_jd, dict) and _jd.get("skipped"):
                _prof.count("js_repos_skipped")
            else:
                _prof.count("js_repos_resolved")
                for _k in ("resolved_imports", "resolved_calls", "resolved_exports"):
                    if isinstance(_jd.get(_k), int):
                        _prof.count("js_" + _k, _jd[_k])
        for _rid, _gd in univ_diags.get("go", {}).items():
            if isinstance(_gd, dict) and _gd.get("skipped"):
                _prof.count("go_repos_skipped")
            else:
                _prof.count("go_repos_resolved")
                for _k in ("resolved_imports", "resolved_calls",
                           "depends_on_derived"):
                    if isinstance(_gd.get(_k), int):
                        _prof.count("go_" + _k, _gd[_k])
        for _ulang in ("rust", "java", "c_sharp", "php", "cpp", "c",
                         "kotlin", "lua", "swift", "dart", "gdscript", "ruby"):
            for _rid, _ud in univ_diags.get(_ulang, {}).items():
                if isinstance(_ud, dict) and _ud.get("skipped"):
                    _prof.count(f"{_ulang}_repos_skipped")
                else:
                    _prof.count(f"{_ulang}_repos_resolved")
        # Phase 15: batched manifest writes (same rows as per-key
        # set_manifest calls, fewer round-trips).
        _manifest_items = []
        for key, res in parsed_results:
            eids = [e.id for e in res.edges]
            for e in new_resolved:
                if edge_owner.get(e.id) == key:
                    eids.append(e.id)
            eids.extend(js_edge_owners.get(key, []))
            eids.extend(py_edge_owners.get(key, []))
            for _ulang in ("go", "rust", "java", "c_sharp", "php", "cpp",
                             "c", "kotlin", "lua", "swift", "dart",
                             "gdscript", "ruby"):
                eids.extend(univ_owners.get(_ulang, {}).get(key, []))
            _manifest_items.append((key, [n.id for n in res.nodes], eids))
        store.set_manifests_batch(_manifest_items)
        # Derived JS edges owned by cached (unparsed) files still need manifests.
        for key, eids in js_edge_owners.items():
            if key == "unknown" or not any(k == key for k, _ in parsed_results):
                for eid in eids:
                    store.cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)",
                                     (key, eid))
        # Same for re-resolved universal-grammar edges owned by cached files.
        for _ulang in ("go", "rust", "java", "c_sharp", "php", "cpp",
                       "c", "kotlin", "lua", "swift", "dart",
                       "gdscript", "ruby"):
            for key, eids in univ_owners.get(_ulang, {}).items():
                if key == "unknown" or \
                        not any(k == key for k, _ in parsed_results):
                    for eid in eids:
                        store.cx.execute(
                            "INSERT OR IGNORE INTO file_edges VALUES(?,?)",
                            (key, eid))
        # Same for re-resolved Python import edges owned by cached files.
        for key, eids in py_edge_owners.items():
            if key == "unknown" or not any(k == key for k, _ in parsed_results):
                for eid in eids:
                    store.cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)",
                                     (key, eid))
        # Same for re-resolved Python edges owned by cached files: without a
        # manifest row the orphan sweep would delete freshly derived facts.
        for e in new_resolved:
            owner = edge_owner.get(e.id)
            if owner and owner != "unknown" and \
                    not any(k == owner for k, _ in parsed_results):
                store.cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)",
                                 (owner, e.id))
        # manifests for renamed-new files are written above (they are new keys, parsed).
        # ---- 5b. Route resolution (framework-aware, Phase 24 Faz C) ----
        # Runs AFTER all manifest writes (it reads the just-published facts
        # and must not precede set_manifests_batch, whose per-key DELETEs
        # would wipe its rows). Writes directly: node updates via upsert
        # (provenance merged, first_seen preserved), new edges via upsert
        # plus their own manifest rows (INSERT OR IGNORE, idempotent).
        # Deterministic: sorted iteration, canonical ids, skip-if-equal.
        _prof.begin("route_resolve")
        from .extraction.code import route_resolve as _rr
        try:
            _route_rids = sorted(
                r["repository_id"] for r in store.cx.execute(
                    "SELECT DISTINCT repository_id FROM nodes "
                    "WHERE type='route'").fetchall() if r["repository_id"])
            try:
                # Django-only projects have no extractor route nodes (entries
                # live in module attrs until the service pass synthesizes
                # them); they must still enter resolution.
                _route_rids = sorted(set(_route_rids) | {
                    r["repository_id"] for r in store.cx.execute(
                        "SELECT DISTINCT repository_id FROM nodes "
                        "WHERE type='module' AND attributes LIKE "
                        "'%django_paths%'").fetchall()
                    if r["repository_id"]})
            except Exception:
                pass
        except Exception:
            _route_rids = []
        for _rid in _route_rids:
            try:
                _rnodes, _rnew, _redges, _rdiag = _rr.resolve_routes(_rid, store)
            except Exception as e:
                errors.append(f"route-resolve {_rid}: {e}")
                continue
            _prof.count("route_nodes_updated", len(_rnodes))
            _prof.count("route_nodes_created", len(_rnew))
            _prof.count("route_test_links", _rdiag.get("test_links", 0))
            if _rnodes:
                store.upsert_nodes(_rnodes)
            for _nokey, _nn in _rnew:
                store.upsert_nodes([_nn])
                store.cx.execute(
                    "INSERT OR IGNORE INTO file_nodes VALUES(?,?)",
                    (_nokey, _nn.id))
            for _okey, _e in _redges:
                store.upsert_edges([_e])
                store.cx.execute(
                    "INSERT OR IGNORE INTO file_edges VALUES(?,?)",
                    (_okey, _e.id))
        _prof.end("route_resolve")
        _prof.begin("tombstones")
        _prof.begin("tombstones")
        # renamed-old keys: tombstone instead of delete (history preserved).
        for old_k, r in zip(sorted(renamed_old), renames):
            _tombstone_rename(store, old_k, r["to"], cfg.project_id)
            store.cx.execute("DELETE FROM files WHERE path=?", (old_k,))
            # drop the old manifest rows so its symbols become candidates for cleanup,
            # but keep the tombstoned file node itself.
            old_nodes = store.get_manifest_nodes(old_k)
            old_edges = store.get_manifest_edges(old_k)
            rn, re_ = _remove_manifest_facts(store, old_k, old_nodes, old_edges)
            removed_nodes += rn
            removed_edges += re_
        # drop resolver edges whose endpoints vanished (stale resolutions)
        _prof.end("tombstones")
        _prof.begin("sweep")
        _drop_dangling_resolver_edges(store)
        # orphans: stale facts from re-parsed files (incl. legacy line-based v1
        # identities superseded by canonical v2 IDs) and unreferenced evidence.
        on, oe, oev = store.sweep_orphans()
        removed_nodes += on
        removed_edges += oe
        _prof.end("sweep")
        _prof.set("sweep_nodes", on)
        _prof.set("sweep_edges", oe)
        # Python import/heritage-aware call binding + superseded-unresolved
        # table: a pure function of the published tables (incremental ==
        # full rebuild), recomputed every non-noop run.
        _prof.begin("py_callbind")
        try:
            from .graph.py_callbind import bind_and_supersede
            _cb = bind_and_supersede(store, dict(repo_roots))
            for _k, _v in _cb.items():
                _prof.set("callbind_" + _k, _v)
        except Exception as exc:
            errors.append(f"py-call-bind: {type(exc).__name__}: {exc}")
        _prof.end("py_callbind")
        # Cross-language HTTP links (frontend request -> backend handler),
        # recomputed from the published tables + sources every run.
        _prof.begin("http_link")
        try:
            from .graph.http_link import link_http
            for _k, _v in link_http(store, dict(repo_roots)).items():
                _prof.set("http_" + _k, _v)
        except Exception as exc:
            errors.append(f"http-link: {type(exc).__name__}: {exc}")
        _prof.end("http_link")
        _prof.begin("type_refs")
        try:
            from .graph.type_refs import link_type_refs
            for _k, _v in link_type_refs(store, dict(repo_roots)).items():
                _prof.set("type_refs_" + _k, _v)
        except Exception as exc:
            errors.append(f"type-refs: {type(exc).__name__}: {exc}")
        _prof.end("type_refs")
        _prof.begin("sql_link")
        try:
            from .graph.sql_link import link_sql
            for _k, _v in link_sql(store, dict(repo_roots)).items():
                _prof.set("sql_" + _k, _v)
        except Exception as exc:
            errors.append(f"sql-link: {type(exc).__name__}: {exc}")
        _prof.end("sql_link")
        _prof.begin("infra_link")
        try:
            from .graph.infra_link import link_infra
            for _k, _v in link_infra(store).items():
                _prof.set("infra_" + _k, _v)
        except Exception as exc:
            errors.append(f"infra-link: {type(exc).__name__}: {exc}")
        _prof.end("infra_link")
        _prof.begin("dispatch_impl")
        try:
            from .graph.dispatch_impl import link_dispatch_impl
            for _k, _v in link_dispatch_impl(store).items():
                _prof.set("dispatch_impl_" + _k, _v)
        except Exception as exc:
            errors.append(f"dispatch-impl: {type(exc).__name__}: {exc}")
        _prof.end("dispatch_impl")
        _prof.begin("dispatch_link")
        try:
            from .graph.dispatch_link import link_dispatch
            for _k, _v in link_dispatch(store, dict(repo_roots)).items():
                _prof.set("dispatch_" + _k, _v)
        except Exception as exc:
            errors.append(f"dispatch-link: {type(exc).__name__}: {exc}")
        _prof.end("dispatch_link")

        # coverage retention: source re-indexing never deletes runs; derived
        # symbol maps for touched files are recomputed and freshness refreshed.
        # Raw coverage observations stay immutable. Best-effort: coverage must
        # never break graph publication.
        _prof.begin("coverage")
        cov_refresh: dict = {}
        try:
            from .coverage import service as _cov_svc
            touched: dict[str, set[str]] = {}
            for key, _res in parsed_results:
                rid = key.split(":", 1)[0]
                touched.setdefault(rid, set()).add(key.split(":", 1)[-1])
            for key in sorted(delete_keys):
                rid = key.split(":", 1)[0]
                touched.setdefault(rid, set()).add(key.split(":", 1)[-1])
            for rid, rels in touched.items():
                if rid in repo_roots:
                    cov_refresh[rid] = _cov_svc.refresh_repo_coverage(
                        store, rid, repo_roots[rid], sorted(rels), commit=False)
        except Exception as exc:
            cov_refresh = {"error": f"{type(exc).__name__}: {exc}"}
        _prof.end("coverage")

        _prof.begin("commit")
        n, e = store.count()
        sha = hashlib.sha256(str(time.time()).encode()).hexdigest()[:12]
        store.cx.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
                         (hashlib.sha256(f"{sha}{time.time()}".encode()).hexdigest()[:12],
                          sha, f"index {total_files} files", time.time(), n, e))
        if scope.get("record_identity"):
            # Bind this database to its workspace on first successful write.
            # Best-effort: identity loss must never fail an otherwise good index.
            try:
                record_scope(store, scope["roots"])
            except Exception as exc:
                errors.append(f"workspace identity not recorded: {exc}")
        try:
            store.cx.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                             (RESOLUTION_MARKER_KEY, _marker))
        except Exception:
            pass
        store.set_generation("ready")
        store.commit()
        _prof.end("commit")
        _prof.begin("summarize")
        dt = round(time.time() - t0, 2)
        deep_parsed = sum(1 for _, r in parsed_results
                          if getattr(r, "extractor", "").startswith("jsts-"))
        fallback_files = _count_parser_status(store, parsed_results, current_keys,
                                              "fallback")
        partial_files = _count_parser_status(store, parsed_results, current_keys, "partial")
        summary = {"files": total_files, "reused": reused, "parsed": len(parsed_results),
                   "nodes": n, "edges": e, "warnings": warnings_total,
                   "workspace": list(scope["roots"]),
                   "workspace_notice": scope.get("notice", ""),
                   "nodes_removed": removed_nodes, "edges_removed": removed_edges,
                   "renames": renames, "ambiguous_renames": ambiguous_renames,
                   "errors": errors[:20], "seconds": dt,
                   "jsts_deep_parsed": deep_parsed, "jsts_fallback_files": fallback_files,
                   "jsts_partial_files": partial_files,
                    "jsts_resolution": js_diag,
                     "py_resolution": py_diag,
                     "go_resolution": univ_diags.get("go", {}),
                     "rust_resolution": univ_diags.get("rust", {}),
                     "java_resolution": univ_diags.get("java", {}),
                     "csharp_resolution": univ_diags.get("c_sharp", {}),
                     "php_resolution": univ_diags.get("php", {}),
                     "cpp_resolution": univ_diags.get("cpp", {}),
                     "c_resolution": univ_diags.get("c", {}),
                     "kotlin_resolution": univ_diags.get("kotlin", {}),
                     "lua_resolution": univ_diags.get("lua", {}),
                     "swift_resolution": univ_diags.get("swift", {}),
                     "dart_resolution": univ_diags.get("dart", {}),
                     "gdscript_resolution": univ_diags.get("gdscript", {}),
                     "capabilities": ts_adapter.capabilities()}
        _prof.end("summarize")
        if profile:
            summary["stage_profile"] = _prof.report()
        store.close()
        lock.release()
        return summary
    except Exception:
        STATUS = {"state": "failed"}
        if profile:
            STATUS["stage_profile"] = _prof.report()
        try:
            store.rollback()
        except Exception:
            pass
        try:
            # Keep last valid graph: fresh connection only flips the marker.
            s2 = SQLiteStore(cfg.storage_path)
            try:
                s2.set_generation("failed")
                s2.commit()
            finally:
                s2.close()
        except Exception:
            pass
        try:
            store.close()
        except Exception:
            pass
        try:
            lock.release()
        except Exception:
            pass
        raise


def _owned_only_by(item_id: str, store, exclude: set[str], kind: str) -> bool:
    """True when every manifest owning this node/edge is in the excluded set
    (i.e. it is a ghost of a deleted/renamed-away file). Items with no manifest
    rows at all (legacy, pre-manifest) return False — they are handled by the
    orphan sweep, never hidden from resolution."""
    if kind == "node":
        rows = store.cx.execute("SELECT file_key FROM file_nodes WHERE node_id=?",
                                (item_id,)).fetchall()
    else:
        rows = store.cx.execute("SELECT file_key FROM file_edges WHERE edge_id=?",
                                (item_id,)).fetchall()
    owners = [r["file_key"] for r in rows]
    return bool(owners) and all(k in exclude for k in owners)


DERIVED_EDGE_METHODS = ("name-resolver", "jsts-resolve")


# Every univ-family resolver: their derived edges are recomputed on each
# resolution run, so stored ones must be purged first (a resolver change or
# a removed call would otherwise leave stale edges on reused files)
_UNIV_RESOLVE_METHODS = ("go-resolve", "rust-resolve", "java-resolve", "csharp-resolve",
                         "php-resolve", "cpp-resolve", "kotlin-resolve", "lua-resolve",
                         "swift-resolve", "dart-resolve", "gdscript-resolve", "ruby-resolve")
_DERIVED_METHODS = ("name-resolver", "jsts-resolve", "py-import-resolve", "py-module-import",
                    "route-resolve") + _UNIV_RESOLVE_METHODS


def _purge_derived_edges(store, keep_repos: set[str] | None = None,
                           keep_js_repos: set[str] | None = None,
                           keep_univ_repos: set[str] | None = None) -> int:
    """Delete stored derived edges + their manifest rows. They are re-derived
    deterministically below; purging avoids version-drift duplicates.

    Phase 14 (scoped invalidation): `name-resolver` edges are always fully
    purged (global re-derivation follows). `py-import-resolve` edges survive
    only with a manifest owner in `keep_repos` (untouched, marker-compatible
    repos whose resolver is skipped). `jsts-resolve` edges additionally
    survive with an owner in `keep_js_repos` (repos with no js-family change
    whose js resolver is skipped). Phase 38: `go-resolve` edges survive
    with an owner in `keep_univ_repos` (same rule for univ families;
    Phase 39 adds `rust-resolve`/`java-resolve`).
    Owner-less derived edges are always purged (stale by construction).
    Without keep sets this is the historical full purge.
    """
    _in = ",".join(f"'{m}'" for m in _DERIVED_METHODS)
    rows = store.cx.execute(
        f"SELECT id, method FROM edges WHERE method IN ({_in})").fetchall()
    if not keep_repos and not keep_js_repos and not keep_univ_repos:
        ids = [r["id"] for r in rows]
    else:
        keep_repos = keep_repos or set()
        keep_js_repos = keep_js_repos or set()
        keep_univ_repos = keep_univ_repos or set()
        # Phase 15 (read-only): owner repos only for derived-method edges,
        # not the whole manifest table. Same keep decisions, fewer rows.
        owner_repo: dict[str, set[str]] = {}
        for r in store.cx.execute(
                "SELECT fe.edge_id, fe.file_key FROM file_edges fe "
                "JOIN edges e ON e.id = fe.edge_id "
                f"WHERE e.method IN ({_in})").fetchall():
            owner_repo.setdefault(r["edge_id"], set()).add(
                r["file_key"].split(":", 1)[0])
        ids = []
        for r in rows:
            if r["method"] == "name-resolver":
                ids.append(r["id"])
                continue
            repos = owner_repo.get(r["id"], set())
            if r["method"] in ("py-import-resolve", "py-module-import",
                               "route-resolve"):
                if not (repos & keep_repos):
                    ids.append(r["id"])
            elif r["method"] in _UNIV_RESOLVE_METHODS:
                if not (repos & (keep_repos | keep_univ_repos)):
                    ids.append(r["id"])
            else:  # jsts-resolve
                if not (repos & (keep_repos | keep_js_repos)):
                    ids.append(r["id"])
    # Set-based deletes in chunks (same rows as the old per-edge loop).
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        store.cx.execute(f"DELETE FROM edges WHERE id IN ({q})", chunk)
        store.cx.execute(f"DELETE FROM file_edges WHERE edge_id IN ({q})", chunk)
    return len(ids)


def _remove_manifest_facts(store, file_key, node_ids, edge_ids) -> tuple[int, int]:
    """Remove a file's manifest contributions; delete only unsupported facts.

    A caller referencing a callee in another file must not delete the callee:
    nodes/edges still owned by another manifest survive (shared entities).
    Returns (removed_nodes, removed_edges).
    """
    rn = re_ = 0
    for eid in edge_ids:
        owners = store.other_owners_of_edge(eid, file_key)
        if owners:
            continue
        store.delete_edges([eid])
        re_ += 1
    for nid in node_ids:
        node = store.get_node(nid)
        if node is not None and node.type == "file" and node.attributes.get("tombstoned"):
            continue  # history tombstones survive
        owners = store.other_owners_of_node(nid, file_key)
        if owners:
            continue
        # Only delete file-owned definitions; a node whose file_path belongs to a
        # *different*, still-indexed file is never deleted here.
        store.delete_nodes([nid])
        rn += 1
    return rn, re_


def _tombstone_rename(store, old_key: str, new_rel: str, project_id: str):
    from .core.identifiers import file_node_id
    from .core.provenance import make_evidence
    from .core.models import Evidence
    row = store.get_file(old_key)
    repo = row["repo_id"] if row else ""
    old_rel = old_key.split(":", 1)[-1] if ":" in old_key else old_key
    old_fid = file_node_id(repo, old_rel)
    new_fid = file_node_id(repo, new_rel)
    # Rename event evidence (owned by the rename itself, not the deleted file).
    rev = make_evidence(f"rename:{old_rel}->{new_rel}",
                        f"renamed {old_rel} to {new_rel}", "rename-detector", b"")
    store.upsert_evidence([rev])
    node = store.get_node(old_fid)
    if node is not None:
        node.attributes = dict(node.attributes or {})
        node.attributes["tombstoned"] = True
        node.attributes["renames_to"] = new_rel
        node.valid_until = time.strftime("%Y-%m-%dT%H:%M:%S")
        node.provenance_ids = [rev.id]
        # Replace (not union): the tombstone's history marker is supported by the
        # rename event. Performed with direct SQL to bypass provenance-union upsert.
        import json
        store.cx.execute(
            "UPDATE nodes SET attributes=?, provenance=?, valid_until=? WHERE id=?",
            (json.dumps(node.attributes), json.dumps([rev.id]), node.valid_until, old_fid))
        # Tombstones stay queryable by ID (history) but leave the search index so
        # deleted files stop surfacing in lexical results.
        try:
            store._fts_delete([old_fid])
        except Exception:
            pass
    edge = Edge(canonical_edge_id(old_fid, "renames_to", new_fid, new_rel),
                old_fid, new_fid, "renames_to", "documented", "claimed", None, [rev.id],
                "rename-detector", "1", file_path=new_rel)
    try:
        store.upsert_edges([edge])
        # owned by the new file's manifest so orphan sweeps keep history links
        new_key = f"{repo}:{new_rel}"
        store.cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (new_key, edge.id))
    except Exception:
        pass
    # The old file's own evidence rows are gone with it; shared entities keep only
    # surviving support (swept below). The tombstone above already carries the
    # rename evidence, so it survives the sweep.
    store.delete_evidence_by_source(old_rel)
    _sweep_dangling_provenance(store)


def _sweep_dangling_provenance(store):
    """Drop provenance IDs with no matching evidence row (post-deletion hygiene)."""
    import json
    ev_ids = {r["id"] for r in store.cx.execute("SELECT id FROM evidence").fetchall()}
    if not ev_ids:
        return
    for tbl in ("nodes", "edges"):
        for r in store.cx.execute(f"SELECT id, provenance FROM {tbl}").fetchall():
            try:
                prov = set(json.loads(r["provenance"] or "[]"))
            except Exception:
                continue
            kept = prov & ev_ids
            # Keep non-evidence provenance tokens (defensive: all ours are evidence IDs,
            # but never destroy what we cannot resolve).
            if kept != prov:
                store.cx.execute(f"UPDATE {tbl} SET provenance=? WHERE id=?",
                                 (json.dumps(sorted(kept)), r["id"]))


def _file_parser_status(store, parsed_results, file_key: str) -> str:
    """Parser status for one JS/TS file: new results first, else stored file node."""
    for key, res in parsed_results:
        if key == file_key:
            for n in getattr(res, "nodes", []):
                if n.type == "file":
                    return ((n.attributes or {}).get("parser") or {}).get("status", "")
    rel = file_key.split(":", 1)[-1]
    repo = file_key.split(":", 1)[0]
    from .core.identifiers import file_node_id
    n = store.get_node(file_node_id(repo, rel))
    if n is not None:
        return ((n.attributes or {}).get("parser") or {}).get("status", "")
    return ""


def _count_parser_status(store, parsed_results, current_keys, want: str) -> int:
    from .extraction.code import tree_sitter as tsa
    total = 0
    for key in current_keys:
        rel = key.split(":", 1)[-1]
        if tsa.language_for_extension(Path(rel).suffix) is None:
            continue
        if _file_parser_status(store, parsed_results, key) == want:
            total += 1
    return total


def _drop_dangling_resolver_edges(store):
    rows = store.cx.execute(
        "SELECT id, source, target FROM edges WHERE method IN ('name-resolver','jsts-resolve',"
        "'py-import-resolve','py-module-import','route-resolve','go-resolve',"
        "'rust-resolve','java-resolve','csharp-resolve',"
        "'php-resolve')").fetchall()
    for r in rows:
        if store.get_node(r["source"]) is None or store.get_node(r["target"]) is None:
            store.delete_edges([r["id"]])
            store.cx.execute("DELETE FROM file_edges WHERE edge_id=?", (r["id"],))


def _js_toolchain_versions() -> dict:
    try:
        from importlib.metadata import version, PackageNotFoundError
    except ImportError:
        return {}
    out = {}
    for pkg in ("tree-sitter", "tree-sitter-javascript", "tree-sitter-typescript"):
        try:
            out[pkg] = version(pkg)
        except Exception:
            out[pkg] = "missing"
    return out


def _js_config_state(repo_root: str) -> str:
    from .extraction.code import module_resolution as mr_mod
    from .extraction.code.jsts import JSTS_VERSION
    return mr_mod.config_state(repo_root, JSTS_VERSION, _js_toolchain_versions())


def _univ_toolchain_versions() -> dict:
    try:
        from importlib.metadata import version
    except ImportError:
        return {}
    try:
        from .extraction.code import tree_sitter as _tsa
        lids = list(getattr(_tsa, "UNIV_LANGUAGE_IDS", ("go",)))
    except Exception:
        lids = ["go"]
    out = {}
    # Registry language ids do not always equal wheel suffixes
    # (c_sharp -> tree-sitter-c-sharp).
    pkgs = ["tree-sitter"] + [
        {"c_sharp": "tree-sitter-c-sharp"}.get(lid, f"tree-sitter-{lid}")
        for lid in lids]
    for pkg in pkgs:
        try:
            out[pkg] = version(pkg)
        except Exception:
            out[pkg] = "missing"
    return out


def _univ_config_state(repo_root: str) -> str:
    """Cache-key state for universal-grammar files (Phase 38): engine
    version + grammar wheel versions + go.mod bytes (module-path mapping
    is a resolution input, like tsconfig for jsts). Installed/uninstalled
    flips and engine bumps invalidate univ files without touching other
    caches (mirrors _js_config_state for the js family)."""
    import hashlib
    from pathlib import Path as _P
    from .extraction.code.ts_universal import UNIV_TS_VERSION
    h = hashlib.sha256()
    h.update(f"univ={UNIV_TS_VERSION}".encode())
    for pkg in sorted(_univ_toolchain_versions):
        h.update(f"{pkg}={_univ_toolchain_versions[pkg]}".encode())
    try:
        h.update(b"|gomod:")
        h.update(_P(repo_root, "go.mod").read_bytes()[:65536])
    except OSError:
        h.update(b"|gomod:<missing>")
    return "cfguniv:" + h.hexdigest()[:16]


def _attribute_resolved_owners(new_resolved, unresolved_owner: dict,
                               proxy_edges: list, node_owner: dict,
                               edge_owner: dict, stats: dict | None = None) -> None:
    """Attribute each produced edge to its unresolved source's file.

    First unresolved edge (in `unresolved_owner` insertion order) whose
    source matches wins; otherwise fall back to the source node's owner.
    Phase 11: indexed lookup — a previous triple-nested linear scan over
    `proxy_edges` was cubic in edge count and hung real-world indexing
    (55.8s at ~10k edges, unbounded beyond); this returns identical results.
    Phase 14: owners are indexed by source ONCE per run instead of scanning
    all owners per resolved edge (O(owners + new) expected vs O(new ×
    owners)). Candidate lists preserve `unresolved_owner` insertion order
    and skip proxy-absent owners exactly like the old scan, so the winner,
    the skips, and the fallback are identical. `stats`, when given, records
    `index_size`, `owner_examinations`, and `resolved` for complexity tests.
    """
    proxy_by_id = {getattr(x, "id", None): x for x in proxy_edges}
    by_source: dict = {}
    for ue_id, owner in unresolved_owner.items():
        ue = proxy_by_id.get(ue_id)
        if ue is None:
            continue
        by_source.setdefault(getattr(ue, "source", None), []).append(owner)
    examinations = 0
    for e in new_resolved:
        cands = by_source.get(e.source)
        if cands:
            examinations += 1
            edge_owner[e.id] = cands[0]
        if edge_owner[e.id] == "unknown":
            edge_owner[e.id] = node_owner.get(e.source, "unknown")
    if stats is not None:
        stats.update(index_size=len(unresolved_owner),
                     owner_examinations=examinations,
                     resolved=len(new_resolved))


def forget_repository(store, repo_id: str, project_id: str = "default") -> dict:
    """Deliberately purge one repository's facts (recovery workflow).

    Ownership-based: nodes/edges surviving under another manifest are kept,
    never deleted by label or basename. Coverage runs, plans, and test runs
    of the repo go with it (all repo-scoped); snapshots stay as history.
    Tombstone history nodes are retained and reported. No commit performed;
    the caller commits. Raises ValueError for unknown repositories.
    """
    import json
    import re as _re
    if not _re.fullmatch(r"repo-[0-9a-f]{10}", repo_id or ""):
        raise ValueError(f"not a repository id: {repo_id!r}")
    keys = sorted(k for k in store.list_file_keys() if k.split(":", 1)[0] == repo_id)
    cov_runs = 0
    try:
        cov_runs = len(store.cx.execute(
            "SELECT id FROM coverage_runs WHERE repo_id=?", (repo_id,)).fetchall())
    except Exception:
        cov_runs = 0
    plans = runs = 0
    for tbl, col in (("test_plans", "repo_id"), ("test_runs", "repo_id")):
        try:
            plans_or_runs = len(store.cx.execute(
                f"SELECT {col} FROM {tbl} WHERE {col}=?", (repo_id,)).fetchall())
        except Exception:
            plans_or_runs = 0
        if tbl == "test_plans":
            plans = plans_or_runs
        else:
            runs = plans_or_runs
    if not keys and not cov_runs and not plans and not runs:
        raise ValueError(f"unknown repository: no facts for {repo_id}")
    rn = re_ = kept = tombstones = 0
    for key in keys:
        nodes = store.get_manifest_nodes(key)
        edges = store.get_manifest_edges(key)
        for eid in sorted(edges):
            if store.other_owners_of_edge(eid, key):
                continue
            store.delete_edges([eid])
            re_ += 1
        for nid in sorted(nodes):
            node = store.get_node(nid)
            if node is not None and node.type == "file" and \
                    (node.attributes or {}).get("tombstoned"):
                tombstones += 1
                continue  # rename history is preserved, reported below
            if store.other_owners_of_node(nid, key):
                kept += 1
                continue
            store.delete_nodes([nid])
            rn += 1
        store.cx.execute("DELETE FROM file_nodes WHERE file_key=?", (key,))
        store.cx.execute("DELETE FROM file_edges WHERE file_key=?", (key,))
        store.cx.execute("DELETE FROM files WHERE path=?", (key,))
    # Evidence rows referenced by nothing surviving are safe to drop;
    # anything still referenced (possibly shared across repos) is kept.
    used: set[str] = set()
    for tbl in ("nodes", "edges"):
        try:
            for r in store.cx.execute(f"SELECT provenance FROM {tbl}").fetchall():
                try:
                    used |= set(json.loads(r["provenance"] or "[]"))
                except Exception:
                    pass
        except Exception:
            pass
    ev_gone = 0
    try:
        for r in store.cx.execute("SELECT id FROM evidence").fetchall():
            if r["id"] not in used:
                store.cx.execute("DELETE FROM evidence WHERE id=?", (r["id"],))
                ev_gone += 1
    except Exception:
        pass
    # Repo-scoped derived records go with the repo (guarded: tables are lazy).
    cov_gone = 0
    for tbl, col in (("coverage_symbol_map", "run_id"), ("coverage_lines", "run_id"),
                     ("coverage_branches", "run_id"), ("coverage_functions", "run_id"),
                     ("coverage_contexts", "run_id"), ("coverage_files", "run_id")):
        try:
            rids = [r["id"] for r in store.cx.execute(
                "SELECT id FROM coverage_runs WHERE repo_id=?", (repo_id,)).fetchall()]
            for rid in rids:
                store.cx.execute(f"DELETE FROM {tbl} WHERE {col}=?", (rid,))
        except Exception:
            pass
    try:
        store.cx.execute("DELETE FROM coverage_run_state WHERE run_id IN "
                         "(SELECT id FROM coverage_runs WHERE repo_id=?)", (repo_id,))
    except Exception:
        pass
    try:
        store.cx.execute("DELETE FROM coverage_runs WHERE repo_id=?", (repo_id,))
        cov_gone = cov_runs
    except Exception:
        pass
    for tbl in ("test_plans", "test_runs"):
        try:
            store.cx.execute(f"DELETE FROM {tbl} WHERE repo_id=?", (repo_id,))
        except Exception:
            pass
    try:
        _sweep_dangling_provenance(store)
    except Exception:
        pass
    return {"repo_id": repo_id, "files": len(keys), "nodes_removed": rn,
            "edges_removed": re_, "shared_nodes_kept": kept,
            "tombstones_retained": tombstones, "evidence_pruned": ev_gone,
            "coverage_runs_removed": cov_gone, "plans_removed": plans,
            "test_runs_removed": runs}


def _dict_edge(d: dict):
    from types import SimpleNamespace
    return SimpleNamespace(id=d["id"], source=d["source"], target=d["target"],
                           relation=d["relation"],
                           extraction_method=d.get("method") or "",
                           attributes=_safe_attrs(d.get("attributes")),
                           provenance_ids=_safe_list(d.get("provenance")),
                           file_path=d.get("file_path") or "", start_line=d.get("start_line") or 0)


def _safe_attrs(a):
    import json
    if isinstance(a, dict):
        return a
    try:
        return json.loads(a or "{}")
    except Exception:
        return {}


def _safe_list(a):
    import json
    if isinstance(a, list):
        return a
    try:
        return json.loads(a or "[]")
    except Exception:
        return []


def normalized_snapshot(store) -> dict:
    """Deterministic normalized graph for incremental≡full comparison.

    Excludes wall-clock fields (first_seen/last_seen, evidence timestamps).
    Everything else — identity, provenance, relations, locations, manifests —
    must match exactly.
    """
    import json
    nodes = {}
    for n in store.all_nodes():
        nodes[n.id] = {"type": n.type, "label": n.label, "qualified": n.qualified_name,
                       "project": n.project_id, "repo": n.repository_id, "file": n.file_path,
                       "loc": [n.location.start_line, n.location.start_column,
                               n.location.end_line, n.location.end_column],
                       "lang": n.language, "content_hash": n.content_hash,
                       "attrs": n.attributes, "prov": sorted(n.provenance_ids or []),
                       "schema": n.schema_version}
    edges = {}
    for e in store.all_edges(include_superseded=True):
        prov = e["provenance"]
        if isinstance(prov, str):
            try:
                prov = json.loads(prov)
            except Exception:
                prov = []
        attrs = e["attributes"]
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except Exception:
                attrs = {}
        edges[e["id"]] = {"source": e["source"], "target": e["target"], "relation": e["relation"],
                          "etype": e["evidence_type"], "cclass": e["confidence_class"],
                          "cscore": e["confidence_score"], "prov": sorted(prov or []),
                          "method": e["method"], "version": e["version"], "attrs": attrs,
                          "file": e["file_path"], "line": e["start_line"]}
    ev = {}
    for r in store.cx.execute("SELECT id, source_uri, file_hash, start_line, end_line, method, version, quote, claim_hash, status FROM evidence").fetchall():
        ev[r["id"]] = dict(r)
    manifests = {}
    for r in store.cx.execute("SELECT path, sha256, extractor, extractor_version, config_hash FROM files").fetchall():
        manifests[r["path"]] = {"sha": r["sha256"], "ext": r["extractor"],
                                "ever": r["extractor_version"], "cfg": r["config_hash"]}
    fts_rows = store.cx.execute("SELECT id FROM node_fts").fetchall()
    fts = sorted(x["id"] for x in fts_rows)
    return {"nodes": nodes, "edges": edges, "evidence": ev, "manifests": manifests, "fts": fts}


def update_changed(cfg: RippletConfig) -> dict:
    return index_workspace(cfg)


def embed_workspace(cfg: RippletConfig, model: str = "", progress=None, cancel=None,
                    provider=None) -> dict:
    """Build/refresh the semantic index (explicit opt-in; never automatic).

    Uses the writer lock because it mutates the database. Pure function of
    current graph + embedding config; unchanged units are reused by text hash.
    """
    from .retrieval import semantic as sem
    from .retrieval.embeddings import OllamaEmbeddingProvider, EmbeddingConfig
    from .workspace_scope import check_scope, missing_roots as _missing_roots
    # Embedding writes vectors: same scope binding as indexing. Gone roots
    # skip the gate; downstream scanning reports the incomplete run.
    if _missing_roots(cfg.workspace_roots):
        scope = {"roots": [str(_P(r).resolve()) for r in cfg.workspace_roots],
                 "storage": str(_P(cfg.storage_path).resolve())}
    else:
        scope = check_scope(roots=cfg.workspace_roots,
                            storage_path=cfg.storage_path, intent="embed")
    cfg.workspace_roots = list(scope["roots"])
    cfg.storage_path = scope["storage"]
    lock = IndexLock(lock_path_for(cfg.storage_path),
                     timeout_s=getattr(cfg, "lock_timeout_s", 30.0))
    try:
        lock.acquire()
    except LockError as e:
        raise
    store = SQLiteStore(cfg.storage_path)
    try:
        active_model = model or getattr(cfg, "embedding_model", "")
        if not active_model and provider is not None:
            # An explicitly passed provider is itself the opt-in: infer the
            # model name from it instead of demanding configuration.
            active_model = getattr(provider, "model", "") or \
                getattr(getattr(provider, "config", None), "model", "")
        if not active_model:
            raise ValueError("no embedding model configured: pass --model or set "
                             "embedding_model (e.g. qwen3-embedding:0.6b)")
        if provider is None:
            provider = OllamaEmbeddingProvider(EmbeddingConfig(
                endpoint=getattr(cfg, "embedding_endpoint",
                                 "http://localhost:11434"),
                model=active_model,
                allow_remote=bool(getattr(cfg, "allow_remote_embeddings", False))))
        stats = sem.build_index(store, provider, active_model,
                                progress=progress, cancel=cancel)
        store.commit()
        try:
            from .retrieval.semantic import state_summary
            stats["state"] = state_summary(store)
        except Exception:
            pass
        store.close()
        lock.release()
        return stats
    except Exception:
        try:
            store.rollback()
        except Exception:
            pass
        try:
            store.close()
        except Exception:
            pass
        try:
            lock.release()
        except Exception:
            pass
        raise


def get_status(cfg: RippletConfig) -> dict:
    store = SQLiteStore(cfg.storage_path)
    try:
        n, e = store.count()
        files = store.cx.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
        snaps = list(store.cx.execute("SELECT * FROM snapshots ORDER BY created_at DESC LIMIT 5").fetchall())
        try:
            generation = store.get_generation()
        except Exception:
            generation = "ready"
        try:
            watch_meta = {r["k"]: r["v"] for r in store.cx.execute(
                "SELECT k,v FROM meta WHERE k='watch_state' OR k='last_reconciled'"
                " OR k='last_batch'").fetchall()}
        except Exception:
            watch_meta = {}
        try:
            from .retrieval.semantic import state_summary
            sem_state = state_summary(store)
        except Exception:
            sem_state = []
        return {"state": STATUS.get("state", "ready"), "generation": generation,
                "nodes": n, "edges": e, "tracked_files": files, "storage": cfg.storage_path,
                "semantic": sem_state,
                "watch": {"state": watch_meta.get("watch_state", "ready"),
                          "last_reconciled": watch_meta.get("last_reconciled"),
                          "last_batch": watch_meta.get("last_batch")},
                "snapshots": [{"id": s["id"], "message": s["message"], "at": s["created_at"]} for s in snaps]}
    finally:
        store.close()


def validate_graph(cfg: RippletConfig) -> dict:
    from .extraction.validation import validate_result
    from .core.models import ExtractionResult
    import json
    store = SQLiteStore(cfg.storage_path)
    try:
        nodes = store.all_nodes()
        edges_raw = store.all_edges()
        from .core.models import Edge as E
        edges = [E(e["id"], e["source"], e["target"], e["relation"]) for e in edges_raw]
        errs = validate_result(ExtractionResult(nodes, edges, []))
        ids = {n.id for n in nodes}
        findings: list[dict] = []
        for m in errs:
            findings.append({"severity": "error", "kind": "schema", "detail": m,
                             "hint": "re-index the owning file; check the extractor version"})
        dangling = [e.id for e in edges if e.source not in ids or
                    (e.target not in ids and not str(e.target).startswith("ext:"))]
        for eid in dangling[:50]:
            findings.append({"severity": "error", "kind": "dangling_edge", "id": eid,
                             "hint": "stale resolution or incomplete delete; run update"})
        ev_ids = {r["id"] for r in store.cx.execute("SELECT id FROM evidence").fetchall()}
        for n in nodes:
            bad = [p for p in (n.provenance_ids or []) if p not in ev_ids]
            if bad:
                findings.append({"severity": "error", "kind": "missing_evidence", "id": n.id,
                                 "detail": f"{len(bad)} provenance refs without evidence rows",
                                 "hint": "run update to sweep dangling provenance"})
                break
        # stale manifests: files rows whose key has no nodes/edges at all (except failed files keep file node)
        for r in store.cx.execute("SELECT path FROM files").fetchall():
            nn = store.cx.execute("SELECT COUNT(*) c FROM file_nodes WHERE file_key=?", (r["path"],)).fetchone()["c"]
            if nn == 0:
                findings.append({"severity": "warning", "kind": "empty_manifest", "id": r["path"],
                                 "hint": "file produced no facts or manifest was lost; re-index"})
        # legacy (pre-canonical) identities still present
        legacy = [n.id for n in nodes if (n.schema_version or 1) < 2]
        if legacy:
            findings.append({"severity": "warning", "kind": "legacy_identity",
                             "detail": f"{len(legacy)} nodes with schema_version<2 "
                                       f"(e.g. {legacy[:3]}); line-based IDs",
                             "hint": "run update to migrate to canonical identities"})
        # duplicate canonical identities
        seen: dict[tuple, str] = {}
        for n in nodes:
            if n.type in ("function", "method", "class") and n.qualified_name:
                key = (n.repository_id, n.language, n.type, n.qualified_name,
                       (n.attributes or {}).get("signature", ""))
                if key in seen and seen[key] != n.id:
                    findings.append({"severity": "error", "kind": "duplicate_identity",
                                     "id": n.id, "detail": f"collides with {seen[key]} for {key}",
                                     "hint": "overload discriminator missing; fix extractor"})
                else:
                    seen[key] = n.id
        # FTS disagreement (incl. duplicate FTS rows from pre-Phase-2 upserts)
        try:
            from collections import Counter
            fts_all = [x["id"] for x in store.cx.execute("SELECT id FROM node_fts").fetchall()]
            dupes = sorted([k for k, c in Counter(fts_all).items() if c > 1])
            if dupes:
                findings.append({"severity": "error", "kind": "fts_duplicates",
                                 "detail": f"{len(dupes)} FTS ids with duplicate rows "
                                           f"(e.g. {dupes[:3]}); from REPLACE-into-FTS5 writes",
                                 "hint": "run update to rewrite the search index (delete+insert)"})
            fts_ids = set(fts_all)
            node_ids = set(ids)
            if fts_ids - node_ids:
                findings.append({"severity": "error", "kind": "fts_orphan",
                                 "detail": f"{len(fts_ids - node_ids)} FTS rows without nodes",
                                 "hint": "run update to resync the search index"})
            tombstoned = {n.id for n in nodes if (n.attributes or {}).get("tombstoned")}
            missing = (node_ids - fts_ids) - tombstoned
            if missing:
                findings.append({"severity": "warning", "kind": "fts_missing",
                                 "detail": f"{len(missing)} nodes missing from FTS",
                                 "hint": "run update to resync the search index"})
        except Exception:
            pass
        # incomplete generation
        try:
            if store.get_generation() not in ("ready",):
                findings.append({"severity": "error", "kind": "incomplete_generation",
                                 "detail": f"generation={store.get_generation()}",
                                 "hint": "last update failed; run update to recover"})
        except Exception:
            pass
        return {"findings": findings[:100], "errors": errs[:50], "dangling_edges": dangling[:50],
                "nodes": len(nodes), "edges": len(edges),
                "ok": not [f for f in findings if f["severity"] == "error"]}
    finally:
        store.close()
