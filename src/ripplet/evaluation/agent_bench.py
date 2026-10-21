"""Paired agent-economy benchmark, offline deterministic mode (Phase 16).

Two FROZEN policies answer every task; nothing is tuned per task:

- condition A (baseline, no Ripplet): identifier-term grep over the
  snapshot + full reads of the top-K hit files (K=4, 64KB cap/file).
- condition B (Ripplet-assisted): a fixed per-category MCP tool plan
  (<=4 calls) through the public `call_tool` interface on a freshly built
  index of the same snapshot.

Scoring is evidence-based: fraction of rubric `must` items (file, plus
symbol when specified) present in the collected evidence; abstain tasks
succeed only on explicit abstain with zero fabricated files. `fail_if`
prose patterns apply to LIVE answers only and are recorded as pending
offline. Token figures here are tiktoken ESTIMATES on tool I/O (stored
separately from any provider-reported usage; see token_accounting).

Live model evaluation lives in tools/benchmarks/live_agent.py (opt-in).
"""
from __future__ import annotations
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

BENCH_VERSION = "agent-tasks-v1"
DATASET_DEFAULT = "tests/evaluation/agent_tasks_v1.json"
EXCLUDE_DIRS = {".ripplet", ".atlasgraph", "__pycache__", ".git", "node_modules"}
EXCLUDE_SUFFIX = {".db", ".db-shm", ".db-wal", ".pyc"}
TOP_K_FILES = 4
FILE_CAP_BYTES = 65536
MAX_TOOL_CALLS = 4

CATEGORY_PLAN = {
    "find_behavior": ["search_symbols", "query_graph"],
    "bug_path": ["query_graph", "find_path", "get_neighbors"],
    "dependencies": ["search_symbols", "get_neighbors"],
    "plan_change": ["query_graph", "analyze_change_impact"],
    "test_select": ["find_related_tests", "query_graph"],
    "architecture": ["query_graph", "search_documents"],
    "unresolved": ["search_symbols", "query_graph"],
    "refactor": ["search_symbols", "get_neighbors", "query_graph"],
}
# Categories whose questions are about file-level dependency structure:
# file nodes own depends_on/contains, so they seed better than modules.
FILE_SEED_CATEGORIES = {"dependencies", "refactor", "plan_change",
                        "test_select"}


def load_dataset(path: str = DATASET_DEFAULT) -> dict:
    ds = json.loads(Path(path).read_text(encoding="utf-8"))
    assert ds.get("version") == BENCH_VERSION, "dataset version mismatch"
    return ds


def snapshot_corpus(src: Path, dest: Path) -> dict:
    """Copy a fixture tree minus volatile files; return {rel: sha} manifest."""
    import hashlib
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src).as_posix()
        parts = set(rel.split("/"))
        if parts & EXCLUDE_DIRS:
            continue
        if p.suffix in EXCLUDE_SUFFIX:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = p.read_bytes()
        target.write_bytes(data)
        manifest[rel] = hashlib.sha256(data).hexdigest()[:16]
    return manifest


def check_integrity(ds: dict, repo_root: Path) -> list[str]:
    """Static dataset checks (no indexing). Returns error strings."""
    errs = []
    ids = [t["id"] for t in ds["tasks"]]
    if len(set(ids)) != len(ids):
        errs.append("duplicate task ids")
    dev, held = set(ds["split"]["dev"]), set(ds["split"]["held"])
    if set(ids) != dev | held:
        errs.append("split does not cover all tasks")
    if dev & held:
        errs.append("dev/held overlap")
    for t in ds["tasks"]:
        corp = repo_root / ds["corpora"][t["corpus"]]
        if not corp.is_dir():
            errs.append(f"{t['id']}: corpus dir missing")
            continue
        for m in t.get("must", []) + t.get("locations", []):
            if not (corp / m["file"]).exists():
                errs.append(f"{t['id']}: rubric file missing {m['file']}")
    return errs


def query_terms(text: str) -> list[str]:
    from ..retrieval import terms as termex
    tex = termex.extract_terms(text)
    seen, out = set(), []
    for tok in (tex.get("identifiers") or []) + (tex.get("keywords") or []):
        low = tok.lower()
        if len(low) >= 3 and low not in seen:
            seen.add(low)
            out.append(tok)
        # Path-like tokens also contribute their file stem: symbol tools
        # resolve names, not paths ("pkg/api.py" -> "api"). Uniform rule.
        if "/" in tok:
            stem = tok.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if len(stem) >= 3 and stem.lower() not in seen:
                seen.add(stem.lower())
                out.append(stem)
    return out[:8]


CODE_EXTS = (".py", ".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx")


def clean_symbol_terms(terms: list[str]) -> list[str]:
    """Symbol-tool terms: path tokens contribute their basename stem (and a
    dotted token its last part); plain tokens pass through; extensions are
    stripped. Order kept, uniform rule, no per-task logic."""
    out, seen = [], set()

    def _add(tok: str):
        if tok and len(tok) >= 3 and tok.lower() not in seen:
            seen.add(tok.lower())
            out.append(tok)

    for tok in terms:
        base = tok
        for ext in CODE_EXTS:
            if base.lower().endswith(ext):
                base = base[:len(base) - len(ext)]
                break
        if "/" in base:
            _add(base.rsplit("/", 1)[-1])
            continue
        _add(base)
        if "." in base and not base.startswith("."):
            _add(base.rsplit(".", 1)[-1])
    return out


def path_tokens(request: str) -> list[str]:
    """File paths and dotted module references read off the raw request
    text (the term extractor splits them into fragments). Deterministic,
    uniform, no per-task logic."""
    out = re.findall(r"[A-Za-z_][\w.\-/]*\.(?:py|js|cjs|mjs|ts|tsx|jsx)\b",
                     request)
    out += [m for m in re.findall(r"\b[a-z_][\w]*\.(?:[a-z_][\w]*\.?)+", request)
            if m not in out]
    return out


def pick_seed(store, request: str, terms: list[str], file_first: bool = False):
    """Deterministic seed choice mirroring the comparative harness: a
    path-like request token prefers a file/module node (basename match),
    otherwise the first symbol hit wins. Dependency-structure categories
    prefer file nodes (they own depends_on/contains). No per-task branching."""
    pathish = path_tokens(request)
    if pathish:
        stem = pathish[0].rsplit("/", 1)[-1]
        for ext in CODE_EXTS:
            if stem.lower().endswith(ext):
                stem = stem[:len(stem) - len(ext)]
                break
        tries = [stem]
        if "." in stem:
            tries.append(stem.rsplit(".", 1)[-1])
        cands = []
        for t in tries:
            cands = store.search_symbols(t, 50, partial=True)
            if cands:
                break
        order = {"file": 0, "module": 1} if file_first else {"module": 0, "file": 1}
        ranked = sorted(
            cands, key=lambda n: (order.get(
                getattr(n, "type", ""), 2),
                1 if getattr(n, "type", "" ) == "external_symbol" else 0,
                len(getattr(n, "qualified_name", "") or "")))
        if ranked:
            return ranked[0]
    return None


def is_uncertainty_marker(item: dict) -> bool:
    """An evidence item that reports uncertainty rather than a verified
    local fact: external placeholders, unresolved markers, or explicit
    no-match limitations surfaced as items."""
    sym = str(item.get("symbol") or "") + str(item.get("label") or "")
    typ = str(item.get("type") or "")
    return (typ == "external_symbol" or sym.startswith("ext:") or
            sym.startswith("import:") or "unresolved" in sym.lower()
            or not item.get("file"))


def run_baseline(task: dict, snap_dir: Path, trace) -> dict:
    """Condition A: grep + bounded full-file reads. Deterministic."""
    from . import token_accounting as ta
    t0 = time.perf_counter()
    terms = query_terms(task["request"])
    ta.record_tool_call(trace, "extract_terms", task["request"],
                        json.dumps(terms))
    files = sorted(p for p in snap_dir.rglob("*") if p.is_file()
                   and p.suffix in (".py", ".js", ".cjs", ".mjs", ".ts",
                                    ".tsx", ".jsx", ".md", ".json", ".cfg",
                                    ".yaml", ".yml"))
    scored: dict[str, int] = {}
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        hits = sum(len(re.findall(r"\b%s\b" % re.escape(t), text))
                   for t in terms)
        if hits:
            scored[fp.relative_to(snap_dir).as_posix()] = hits
    ranked = sorted(scored, key=lambda f: (-scored[f], f))[:TOP_K_FILES]
    ta.record_tool_call(trace, "grep_repo", json.dumps(terms),
                        json.dumps(ranked))
    evidence = []
    for rel in ranked:
        try:
            text = (snap_dir / rel).read_text(encoding="utf-8",
                                              errors="replace")[:FILE_CAP_BYTES]
        except Exception:
            continue
        ta.record_tool_call(trace, "read_file", rel, text)
        evidence.append({"file": rel, "symbol": None, "via": "read_file"})
        for m in re.finditer(r"^(?:def|class)\s+([A-Za-z_]\w*)", text,
                             re.MULTILINE):
            if m.group(1).lower() in {t.lower() for t in terms}:
                evidence.append({"file": rel, "symbol": m.group(1),
                                 "via": "read_file"})
    return {"evidence": evidence, "abstained": not evidence,
            "seconds": round(time.perf_counter() - t0, 4)}


def _ripplet_items(call_name: str, res, evidence: list, store=None) -> None:
    if isinstance(res, dict) and res.get("error"):
        return
    items = []
    if isinstance(res, dict):
        items = res.get("facts") or res.get("items") or []
        if not items and isinstance(res.get("node"), dict):
            items = [res["node"]]
    elif isinstance(res, list):
        for e in res:
            if not isinstance(e, dict):
                continue
            if not (e.get("source") and e.get("target")):
                # Node/fact dicts pass through untouched (edge resolution
                # below applies to edge-shaped dicts only).
                items.append(e)
                continue
            if store is None:
                items.append(e)
                continue
            # Edge-list results (get_neighbors): resolve endpoint ids to nodes.
            for side in ("source", "target"):
                try:
                    n = store.get_node(e.get(side, ""))
                except Exception:
                    n = None
                if n is not None:
                    items.append({"file": getattr(n, "file_path", ""),
                                  "symbol": getattr(n, "label", ""),
                                  "type": getattr(n, "type", "")})
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        f = it.get("file")
        if not f:
            # File-less uncertainty markers (external/unresolved placeholders)
            # are kept as evidence of reported uncertainty; the fabrication
            # check only counts non-empty files outside the snapshot.
            probe = {"file": "", "symbol": it.get("symbol") or it.get("label"),
                     "type": it.get("type"), "via": call_name}
            if is_uncertainty_marker(probe):
                evidence.append(probe)
            continue
        evidence.append({"file": f,
                         "symbol": it.get("symbol") or it.get("label"),
                         "via": call_name,
                         "id": it.get("id", "")})


def ident_records(call_name: str, res, store) -> list:
    """Observation-only per-call identity records for audit (Phase 17).

    Each record carries the stable identity (node/edge id where present),
    file/symbol/type, and rendered byte size. Never affects policy or
    scoring; consumed only when the caller passes record_calls=[...].
    """
    out = []
    if isinstance(res, dict) and res.get("error"):
        return out
    items = []
    if isinstance(res, dict):
        items = res.get("facts") or res.get("items") or []
        if not items and isinstance(res.get("node"), dict):
            items = [res["node"]]
        if not items and isinstance(res.get("ranked_paths"), list):
            for path in res["ranked_paths"][:3]:
                for nid in (path if isinstance(path, list) else []):
                    items.append({"id": nid})
    elif isinstance(res, list):
        items = res
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            out.append({"key": ("id", it), "id": it, "file": "",
                        "symbol": "", "type": "", "bytes": len(it)})
            continue
        if not isinstance(it, dict):
            continue
        nid = it.get("id", "")
        node = None
        if nid and store is not None and not str(nid).startswith("ext:"):
            try:
                node = store.get_node(nid)
            except Exception:
                node = None
        if node is not None:
            out.append({"key": ("node", nid),
                        "id": nid, "file": getattr(node, "file_path", ""),
                        "symbol": getattr(node, "label", ""),
                        "qualified": getattr(node, "qualified_name", ""),
                        "type": getattr(node, "type", ""),
                        "provenance": sorted(getattr(node, "provenance_ids", "")
                                             or []),
                        "bytes": len(json.dumps(it))})
        elif it.get("source") and it.get("target"):
            out.append({"key": ("edge", it.get("id", "")),
                        "id": it.get("id", ""),
                        "source": it.get("source"), "target": it.get("target"),
                        "relation": it.get("relation", ""),
                        "file": it.get("file", ""),
                        "symbol": it.get("symbol", ""),
                        "bytes": len(json.dumps(it))})
        else:
            out.append({"key": ("fact", str(it.get("file", "")),
                                str(it.get("symbol") or it.get("label", ""))),
                        "id": nid, "file": it.get("file", ""),
                        "symbol": it.get("symbol") or it.get("label", ""),
                        "type": it.get("type", ""),
                        "bytes": len(json.dumps(it))})
    return out


LEDGER_TOOLS = {"search_symbols", "query_graph", "get_neighbors"}


MAX_EXPANSIONS = 8


def run_ripplet(task: dict, store, cfg, trace, record_calls: list | None = None,
              ledger: dict | None = None,
              pack_format: str | None = None,
              spans: bool = False, expand: str | None = None) -> dict:
    """Condition B: frozen per-category MCP plan (<=4 calls). Deterministic:
    no per-task branching beyond the category template and term fallback.
    `record_calls`, when a list, receives per-call identity records for
    audit (policy and scoring unchanged).
    `ledger`, when a dict {"snapshot": id, "ids": [...]}, is passed as the
    `known` argument to ledger-wired tools and extended in place with every
    returned stable id (condition C). Cited refs score like the items they
    cite, since refs carry file/symbol.
    `pack_format` (condition D, Phase 18): query_graph calls request the
    packed presentation and decode before evidence extraction. Recorded tool
    outputs are the packed bytes (the measured saving); decode failures
    surface as call errors, never silent downgrades.
    `spans` (condition E, Phase 19): query_graph calls add
    evidence_mode="spans". `expand="all"` then expands every initial
    evidence id once at symbol level (bounded by MAX_EXPANSIONS, label-free
    upper-bound policy; recorded with tokens and latency)."""
    from . import token_accounting as ta
    from ..mcp import tools as mcptools
    t0 = time.perf_counter()
    evidence: list = []
    calls = 0

    ledger_full: dict[str, dict] = {}

    def _call(name: str, args: dict):
        nonlocal calls
        if calls >= MAX_TOOL_CALLS:
            return None
        calls += 1
        t1 = time.perf_counter()
        args = dict(args)
        if ledger is not None and name in LEDGER_TOOLS and ledger.get("ids"):
            # An empty ledger matches nothing: skip the arg (and its echo
            # + envelope overhead) until the caller actually holds ids.
            args["known"] = {"snapshot": ledger.get("snapshot", ""),
                             "ids": list(ledger.get("ids", []))}
        if pack_format is not None and name == "query_graph":
            args["format"] = pack_format
        if spans and name == "query_graph":
            args["evidence_mode"] = "spans"
        try:
            res = mcptools.call_tool(store, cfg, name, args)
        except Exception as e:
            res = {"error": f"{type(e).__name__}: {e}"}
        if pack_format is not None and isinstance(res, dict) and \
                res.get("format") == pack_format:
            try:
                from ..query.factpack import decode as _unpack
                res = _unpack(res)
            except Exception as e:
                res = {"error": f"factpack-decode:{type(e).__name__}: {e}"}
        secs = round(time.perf_counter() - t1, 4)
        ta.record_tool_call(trace, f"ripplet:{name}", json.dumps(args),
                            json.dumps(res)[:20000])
        # Ledger-backed callers resolve refs to the full first-seen content
        # for their own evidence tracking (deduped by id); the wire saving
        # is measured on the recorded tool output above, not here.
        items = res if isinstance(res, list) else (
            res.get("results", res.get("facts", []))
            if isinstance(res, dict) else [])
        resolved = []
        for it in items or []:
            if isinstance(it, dict) and it.get("omitted") and \
                    it.get("ref") in ledger_full:
                full = ledger_full[it["ref"]]
                resolved.append(full)
            else:
                resolved.append(it)
        _ripplet_items(name, resolved, evidence, store)
        for it in items or []:
            if isinstance(it, dict) and not it.get("omitted"):
                key = it.get("id", "")
                if key and key not in ledger_full:
                    ledger_full[key] = it
        if ledger is not None and isinstance(res, (dict, list)):
            items = res if isinstance(res, list) else res.get("results", res.get("facts", []))
            for it in items or []:
                if not isinstance(it, dict) or it.get("omitted"):
                    continue
                key = it.get("id", "")
                if not key:
                    typ = str(it.get("type", "") or "")
                    sym = str(it.get("symbol") or it.get("label") or "")
                    if typ == "external_symbol" or sym.startswith("ext:") or \
                            sym.startswith("import:"):
                        key = f"marker:{typ}:{sym}"
                if key and key not in ledger["ids"]:
                    ledger["ids"].append(key)
        if record_calls is not None:
            record_calls.append({"call": name, "args": args,
                                 "items": ident_records(name, res, store)})
        return res

    terms = query_terms(task["request"])
    sym_terms = clean_symbol_terms(terms)
    top_id = terms[0] if terms else task["request"].split()[0]
    plan = CATEGORY_PLAN.get(task["category"], ["search_symbols", "query_graph"])
    top_hit = None
    dual_seed = task["category"] in FILE_SEED_CATEGORIES
    seed = pick_seed(store, task["request"], terms, file_first=False)
    file_seed = (pick_seed(store, task["request"], terms, file_first=True)
                 if dual_seed else None)
    if file_seed is not None and seed is not None and file_seed.id == seed.id:
        file_seed = None
    def _result_items(res):
        # Ledger-wrapped responses ({results,...}) decide like the legacy
        # list they replace; refs (omitted) never seed.
        if isinstance(res, dict):
            res = res.get("results", [])
        return [it for it in res if isinstance(it, dict)
                and not it.get("omitted")] if isinstance(res, list) else []

    for op in plan:
        if op == "search_symbols":
            # Uniform identifier sweep: try identifiers in order until one
            # returns hits (still within the call budget via _call's gate).
            for cand in terms[:2]:
                res = _call("search_symbols", {"query": cand, "limit": 10})
                hits = _result_items(res)
                if hits:
                    top_hit = hits[0]
                    break
        elif op == "query_graph":
            _call("query_graph", {"compact": False, "query": task["request"], "limit": 10})
        elif op == "get_neighbors":
            # Dependency-structure questions seed both the file node
            # (depends_on/contains) and the module/symbol hit (imports and
            # definitions); either may carry the needed evidence.
            targets = []
            if file_seed is not None:
                targets.append({"id": file_seed.id})
            if seed is not None:
                targets.append({"id": seed.id})
            if not targets and isinstance(top_hit, dict) and top_hit.get("id"):
                targets.append(top_hit)
            for target in targets:
                # Legacy row shape pinned (compat flag), like find_related_tests.
                _call("get_neighbors", {"id": target["id"], "direction": "both",
                                        "limit": 50, "compact": False})
        elif op == "find_path" and len(sym_terms) >= 2:
            _call("find_path", {"source": sym_terms[0], "target": sym_terms[1],
                                "max_depth": 6})
        elif op == "find_related_tests":
            # Legacy bare-id shape pinned via the documented compat flag,
            # so offline benchmark history is byte-identical post-flip.
            _call("find_related_tests", {"symbols": sym_terms[:5],
                                          "compact": False})
        elif op == "search_documents":
            _call("search_documents", {"query": task["request"], "limit": 10})
        elif op == "analyze_change_impact":
            _call("analyze_change_impact", {"symbols": sym_terms[:5], "files": [],
                                            "max_depth": 4, "compact": False})
    if not evidence and calls < MAX_TOOL_CALLS:
        # Universal fallback: a natural-language query often succeeds where
        # a bare identifier found nothing (or vice versa).
        _call("query_graph", {"compact": False, "query": task["request"], "limit": 10})
    n_expanded = 0
    if expand == "all":
        # Bounded label-free upper bound: expand every initial evidence id
        # once at symbol level. Counted in tokens/latency like any call.
        seen_ids: set[str] = set()
        for e in list(evidence):
            eid = str((e or {}).get("id", "") or "") if isinstance(e, dict) else ""
            if not eid or eid in seen_ids or n_expanded >= MAX_EXPANSIONS:
                continue
            seen_ids.add(eid)
            _call("expand_evidence", {"evidence_id": eid, "level": "symbol"})
            n_expanded += 1
    # Abstain iff nothing was evidenced, or every item is an uncertainty
    # marker (placeholders / unresolved reports with no verified local
    # fact). The marker itself is kept in evidence for audit.
    abstained = (not evidence) or (
        bool(evidence) and all(is_uncertainty_marker(e) for e in evidence))
    return {"evidence": evidence, "abstained": abstained,
            "expansions": n_expanded if expand else 0,
            "seconds": round(time.perf_counter() - t0, 4)}


def score_task(task: dict, result: dict, manifest: set[str]) -> dict:
    must = task.get("must", [])
    ev = result.get("evidence", [])
    hits = 0.0
    for m in must:
        best = 0.0
        for e in ev:
            if e.get("file") != m["file"]:
                continue
            if m.get("symbol") and (e.get("symbol") or "") != m["symbol"]:
                # Right file, wrong symbol: partial credit (the reference
                # was surfaced, not the exact definition).
                best = max(best, 0.5)
                continue
            best = 1.0
            break
        hits += best
    fabricated = sorted({e.get("file", "") for e in ev
                         if e.get("file") and e["file"] not in manifest})
    if task.get("abstain_expected"):
        entity = str(task.get("entity") or "")
        markers = [e for e in ev if is_uncertainty_marker(e)]
        verified_entity_claim = any(
            (e.get("symbol") or "") == entity or
            (entity and entity in (e.get("file") or "") and e.get("symbol"))
            for e in ev if not is_uncertainty_marker(e)) if entity else False
        # Success: pure abstain, or uncertainty markers for the named entity
        # with no verified claim about it and nothing fabricated.
        ok = (not fabricated) and (
            bool(result.get("abstained")) or
            (bool(markers) and not verified_entity_claim))
    else:
        ok = (hits == len(must)) and not fabricated
    return {"recall": round(hits / len(must), 3) if must else
            (1.0 if result.get("abstained") else 0.0),
            "hits": hits, "of": len(must), "success": ok,
            "abstained": bool(result.get("abstained")),
            "fabricated": fabricated}


def evaluate(dataset: str = DATASET_DEFAULT, repo_root: str = ".",
             tmp_root: str | None = None, split: str = "all",
             tasks: list[str] | None = None,
             conditions: tuple[str, ...] = ("A", "B")) -> dict:
    """Offline paired run. One fresh index per corpus; all conditions read
    the same snapshot. Condition C = B-policy with caller ledger chaining
    (Phase 17). Returns machine-readable results + accounting."""
    from . import token_accounting as ta
    from ..config import RippletConfig
    from ..service import index_workspace
    from ..storage.sqlite_store import SQLiteStore
    ds = load_dataset(dataset)
    root = Path(repo_root)
    tmp = Path(tmp_root or tempfile.mkdtemp(prefix="agent-bench-"))
    sel = [t for t in ds["tasks"]
           if (split == "all" or t["id"] in ds["split"].get(split, []))
           and (tasks is None or t["id"] in tasks)]
    # One snapshot + index per corpus (index cost attributed per task below).
    corp_info = {}
    for cname, crel in ds["corpora"].items():
        snap = tmp / f"snap-{cname}"
        manifest = snapshot_corpus(root / crel, snap)
        db = tmp / f"{cname}.db"
        t0 = time.perf_counter()
        index_workspace(RippletConfig([str(snap)], str(db), "bench"))
        index_s = round(time.perf_counter() - t0, 4)
        st = SQLiteStore(str(db))
        cfg = RippletConfig([str(snap)], str(db), "bench")
        corp_info[cname] = {"snap": snap, "manifest": manifest, "store": st,
                            "cfg": cfg, "index_s": index_s,
                            "db_kb": round(db.stat().st_size / 1024, 1)}
    results = []
    try:
        for task in sel:
            ci = corp_info[task["corpus"]]
            mani = set(ci["manifest"])
            for cond in conditions:
                trace = ta.new_trace(task["id"], cond)
                t0 = time.perf_counter()
                if cond == "A":
                    res = run_baseline(task, ci["snap"], trace)
                elif cond == "C":
                    snap_row = ci["store"].cx.execute(
                        "SELECT id FROM snapshots ORDER BY created_at DESC LIMIT 1").fetchone()
                    ledger = {"snapshot": snap_row["id"] if snap_row else "",
                              "ids": []}
                    res = run_ripplet(task, ci["store"], ci["cfg"], trace,
                                    ledger=ledger)
                elif cond == "D":
                    res = run_ripplet(task, ci["store"], ci["cfg"], trace,
                                    pack_format="fact_pack_v1")
                elif cond == "E":
                    res = run_ripplet(task, ci["store"], ci["cfg"], trace,
                                    pack_format="fact_pack_v1", spans=True,
                                    expand="all")
                else:
                    res = run_ripplet(task, ci["store"], ci["cfg"], trace)
                wall = round(time.perf_counter() - t0, 4)
                sc = score_task(task, res, mani)
                breakdown: dict[str, dict] = {}
                for c in trace["calls"]:
                    if "tool" not in c:
                        continue
                    b = breakdown.setdefault(c["tool"], {"calls": 0,
                                                         "est_in": 0,
                                                         "est_out": 0})
                    b["calls"] += 1
                    b["est_in"] += max(c.get("est_in", 0), 0)
                    b["est_out"] += max(c.get("est_out", 0), 0)
                results.append({"id": task["id"], "category": task["category"],
                                "condition": cond, "evidence": res["evidence"][:25],
                                "expansions": res.get("expansions", 0),
                                "tool_breakdown": breakdown,
                                "n_evidence": len(res["evidence"]),
                                "recall": sc["recall"], "success": sc["success"],
                                "abstained": sc["abstained"],
                                "fabricated": sc["fabricated"],
                                "fail_if_pending": task.get("fail_if", []),
                                "tool_calls": len(trace["calls"]),
                                "est_tokens": trace["estimate_totals"],
                                "wall_s": wall,
                                "index_s": ci["index_s"], "db_kb": ci["db_kb"]})
    finally:
        for ci in corp_info.values():
            try:
                ci["store"].close()
            except Exception:
                pass
    return {"version": BENCH_VERSION, "split": split,
            "corpora": {k: {"index_s": v["index_s"], "db_kb": v["db_kb"],
                            "files": len(v["manifest"])}
                        for k, v in corp_info.items()},
            "results": results}


def summarize(res: dict) -> dict:
    out = {"by_condition": {}, "paired": {}}
    conds = sorted({r["condition"] for r in res["results"]})
    for cond in conds:
        rows = [r for r in res["results"] if r["condition"] == cond]
        n = len(rows)
        succ = sum(1 for r in rows if r["success"])
        out["by_condition"][cond] = {
            "n": n, "success_rate": round(succ / n, 3) if n else 0.0,
            "mean_recall": round(sum(r["recall"] for r in rows) / n, 3) if n else 0.0,
            "mean_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 2) if n else 0.0,
            "mean_est_tokens": round(sum(r["est_tokens"]["input"] + r["est_tokens"]["output"]
                                         for r in rows) / n, 1) if n else 0.0,
            "mean_wall_s": round(sum(r["wall_s"] for r in rows) / n, 3) if n else 0.0,
            "fabricated_total": sum(len(r["fabricated"]) for r in rows)}
    by_id: dict[str, dict] = {}
    for r in res["results"]:
        by_id.setdefault(r["id"], {})[r["condition"]] = r
    both_ok = sum(1 for v in by_id.values()
                  if v.get("A", {}).get("success") and v.get("B", {}).get("success"))
    b_only = [i for i, v in by_id.items()
              if v.get("B", {}).get("success") and not v.get("A", {}).get("success")]
    a_only = [i for i, v in by_id.items()
              if v.get("A", {}).get("success") and not v.get("B", {}).get("success")]
    out["paired"] = {"n": len(by_id), "both_success": both_ok,
                     "b_only": b_only, "a_only": a_only}
    def _compare(x, y, tag):
        # Same policy, different presentation: recall/success must not
        # regress; token deltas are the measured saving.
        pairs = [(v[x], v[y]) for v in by_id.values() if x in v and y in v]
        if not pairs:
            return
        tok = lambda r: r["est_tokens"]["input"] + r["est_tokens"]["output"]
        deltas = [tok(b) - tok(c) for b, c in pairs]
        out["paired"][tag] = {
            "n": len(pairs),
            "b_success": sum(1 for b, _ in pairs if b["success"]),
            "c_success": sum(1 for _, c in pairs if c["success"]),
            "b_recall": round(sum(b["recall"] for b, _ in pairs) / len(pairs), 3),
            "c_recall": round(sum(c["recall"] for _, c in pairs) / len(pairs), 3),
            "median_token_delta": sorted(deltas)[len(deltas) // 2],
            "mean_token_delta": round(sum(deltas) / len(deltas), 1),
            "regressions": [b["id"] for b, c in pairs
                            if (b["success"] and not c["success"])
                            or c["recall"] < b["recall"]]}
    if "B" in conds and "C" in conds:
        _compare("B", "C", "bc")
    if "B" in conds and "D" in conds:
        _compare("B", "D", "bd")
    return out
