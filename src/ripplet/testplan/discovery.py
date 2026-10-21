"""Candidate discovery: graph + coverage -> A/B/C categorized candidates.

- A (verified): `test_covers_symbol` observations with compatible revision.
- B (static): test-file nodes reaching a changed symbol via calls/imports/uses
  (fixture edges included), resolved through the module system.
- C (inferred): reverse-dependency traversal + same-module naming, always with
  an explicit path or heuristic label. Never presented as verified.

Discovery only collects; ranking decides order (ranking.py).
"""
from __future__ import annotations


def _is_test_node(n) -> bool:
    if getattr(n, "type", "") == "test":
        return True
    label = (getattr(n, "label", "") or "")
    fp = (getattr(n, "file_path", "") or "").lower().replace("\\", "/")
    return label.startswith("test_") or "/test_" in fp or "/tests/" in fp


def _fresh_runs(store, repo_id: str) -> list[dict]:
    """Coverage runs usable as current evidence (verified/override only)."""
    out = []
    try:
        rows = store.cx.execute("SELECT * FROM coverage_runs WHERE repo_id=?",
                                (repo_id,)).fetchall()
    except Exception:
        return out
    import json
    for r in rows:
        d = dict(r)
        state = None
        try:
            s = store.cx.execute("SELECT freshness FROM coverage_run_state WHERE run_id=?",
                                 (d["id"],)).fetchone()
            state = s["freshness"] if s else None
        except Exception:
            pass
        d["_fresh"] = state
        out.append(d)
    return out


BUDGETS = {"max_depth": 4, "max_candidates": 500, "max_edges_scanned": 200000}


def discover(store, changed_symbols: dict[str, dict], repo_id: str,
             max_depth: int = 4,
             max_candidates: int = BUDGETS["max_candidates"]
             ) -> tuple[list[dict], list[str], dict]:
    """Returns (candidates, limitations, diagnostics).

    Each candidate carries category, evidence, path, locations and uncertainty
    — everything ranking needs. Candidates are scoped to `repo_id` so identical
    symbol names in other repositories never leak into a plan. Work is bounded
    (depth + candidate cap); exhaustion is reported, never silent.
    """
    from ..graph import impact as _impact
    cands: dict[str, dict] = {}
    limitations: list[str] = []
    diagnostics: dict = {"budget_hit": None, "edges_scanned": 0,
                         "unresolved_edges_seen": 0, "cross_repo_skipped": 0}

    _node_cache: dict[str, object] = {}

    def _cached_node(nid: str):
        if nid not in _node_cache:
            _node_cache[nid] = store.get_node(nid)
        return _node_cache[nid]

    def add(tid: str, cat: str, evidence: dict, path=None, depth: int = 0,
            via: str = "", run_id: str = "", revision_ok: bool | None = None,
            ambiguous_steps: int = 0):
        prev = cands.get(tid)
        order = {"A": 0, "B": 1, "C": 2}
        if prev is not None and order[prev["category"]] <= order[cat]:
            return
        node = _cached_node(tid)
        if node is None:
            return
        if getattr(node, "repository_id", "") != repo_id:
            diagnostics["cross_repo_skipped"] += 1
            return
        if len(cands) >= max_candidates:
            diagnostics["budget_hit"] = f"max_candidates={max_candidates}"
            return
        loc = getattr(node, "location", None)
        cands[tid] = {"test_id": tid, "test": node.qualified_name,
                      "file": node.file_path,
                      "location": {"start_line": getattr(loc, "start_line", 1),
                                   "end_line": getattr(loc, "end_line", 1)},
                      "language": getattr(node, "language", ""),
                      "category": cat,
                      "evidence": evidence, "path": path or [], "depth": depth,
                      "via": via, "run_id": run_id, "revision_ok": revision_ok,
                      "ambiguous_steps": ambiguous_steps}

    sym_ids = list(changed_symbols)
    if not sym_ids:
        return [], ["no changed symbols could be mapped"], diagnostics
    # ---- A: verified observations with compatible revision
    runs = _fresh_runs(store, repo_id)
    try:
        links = [dict(x) for x in store.cx.execute(
            "SELECT * FROM edges WHERE relation='test_covers_symbol'").fetchall()]
    except Exception:
        links = []
    for run in runs:
        ok_rev = run.get("revision_status") in ("verified", "override") and \
            run.get("_fresh") in ("current", None)
        for e in links:
            prov_run = ""
            try:
                import json
                attrs = json.loads(e["attributes"] or "{}")
                prov_run = attrs.get("run_id", "")
            except Exception:
                pass
            if prov_run != run["id"]:
                continue
            if e["target"] in sym_ids:
                add(e["source"], "A",
                    {"kind": "test_covers_symbol", "edge": e["id"], "run_id": run["id"]},
                    path=[e["source"], e["target"]], depth=1,
                    via="observed", run_id=run["id"], revision_ok=ok_rev)
    # ---- B: static test relationships (calls/imports/uses/fixture from tests)
    # Targeted reads (rowid order matches the legacy full scans); sources
    # resolve through one batched node fetch instead of per-edge get_node.
    _rels = ("calls", "imports", "uses", "imports_from", "depends_on", "calls_http")
    in_edges = store.edges_by_targets(sym_ids, _rels)
    by_target: dict[str, list] = {}
    for e in in_edges:
        by_target.setdefault(e["target"], []).append(e)
    _node_cache.update(store.nodes_by_ids(
        {e["source"] for es in by_target.values() for e in es}))
    for sid in sym_ids:
        for e in by_target.get(sid, []):
            src = _cached_node(e["source"])
            if src is None or not _is_test_node(src):
                continue
            # resolve through import aliases: test -> ext placeholder -> real
            # symbol chains count when the placeholder is unambiguous
            add(src.id, "B",
                {"kind": e["relation"], "edge": e["id"],
                 "location": [e["file_path"], e["start_line"]]},
                path=[src.id, sid], depth=1, via="static")
    # fixture uses from tests toward helpers near the change
    uses_edges = store.edges_by_relation("uses")
    import json as _json
    _node_cache.update(store.nodes_by_ids(
        {e["source"] for e in uses_edges} |
        {e["target"] for e in uses_edges}))
    for e in uses_edges:
        try:
            attrs = _json.loads(e["attributes"] or "{}")
        except Exception:
            attrs = {}
        if not attrs.get("fixture"):
            continue
        src = _cached_node(e["source"])
        tgt = _cached_node(e["target"])
        if src is None or not _is_test_node(src):
            continue
        if tgt is not None and tgt.id in sym_ids:
            add(src.id, "B", {"kind": "uses_fixture", "edge": e["id"]},
                path=[src.id, tgt.id], depth=1, via="static")
    # ---- C: transitive dependents via impact traversal
    try:
        findings = _impact.analyze(sym_ids, store.edge_tuples(),
                                   max_depth=max_depth)
    except Exception:
        findings = []
        limitations.append("impact traversal failed; transitive candidates unavailable")
    try:
        diagnostics["edges_scanned"] = min(store.count()[1], 100000)
    except Exception:
        diagnostics["edges_scanned"] = len(in_edges) + len(uses_edges)
    pair_need: set[tuple[str, str]] = set()
    for f in findings:
        via = f.get("via", []) or []
        for a, b in zip(via, via[1:]):
            pair_need.add((a, b))
            pair_need.add((b, a))
    edge_by_pair = {}
    if pair_need:
        for e in store.edges_by_sources(
                sorted({a for a, _ in pair_need})):
            if (e["source"], e["target"]) in pair_need:
                edge_by_pair[(e["source"], e["target"])] = e
    _node_cache.update(store.nodes_by_ids(
        {f.get("affected") for f in findings if f.get("affected")}))
    unresolved_rels = {"calls_unresolved"}
    for f in findings:
        aff = f.get("affected")
        n = _cached_node(aff) if aff else None
        if n is None or not _is_test_node(n):
            continue
        if aff in cands:
            continue
        via = f.get("via", []) or []
        amb = 0
        for a, b in zip(via, via[1:]):
            e = edge_by_pair.get((a, b)) or edge_by_pair.get((b, a))
            if e is not None and e["relation"] in unresolved_rels:
                amb += 1
            if e is not None and "ambiguous" in str(e.get("attributes", "")):
                amb += 1
        diagnostics["unresolved_edges_seen"] += amb
        add(aff, "C", {"kind": "impact_path", "via_path": via,
                       "confidence": f.get("confidence", "hypothetical"),
                       "ambiguous_steps": amb},
            path=via, depth=f.get("depth", 99), via="inferred",
            ambiguous_steps=amb)
    # ---- C: same-module naming heuristic (explicitly labeled, lowest priority)
    try:
        import os
        changed_files = {v.get("file", "") for v in changed_symbols.values()}
    except Exception:
        changed_files = set()
    for n in store.test_candidate_nodes(repo_id):
        if not _is_test_node(n) or n.id in cands:
            continue
        if getattr(n, "repository_id", "") != repo_id:
            continue
        fp = (n.file_path or "").replace("\\", "/")
        stem = fp.rsplit("/", 1)[-1]
        hit = any(cf and (cf == fp or stem.replace("test_", "").replace("_test", "")
                          in cf or cf.replace("/", "") in stem.replace("/", ""))
                  for cf in changed_files)
        if hit:
            add(n.id, "C", {"kind": "naming_heuristic",
                            "note": "same-module/test-name similarity only"},
                path=[], depth=99, via="inferred")
    if not cands:
        limitations.append("no test candidates discovered for the mapped symbols")
    if diagnostics.get("cross_repo_skipped"):
        limitations.append(
            f"{diagnostics['cross_repo_skipped']} cross-repository test(s) with "
            "identical names were excluded by repository scope")
    if diagnostics.get("budget_hit"):
        limitations.append(f"candidate discovery stopped at budget: "
                           f"{diagnostics['budget_hit']}")
    return list(cands.values()), limitations, diagnostics
