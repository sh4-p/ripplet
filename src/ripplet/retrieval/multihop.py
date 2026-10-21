"""Endpoint-aware multi-hop path retrieval (Phase 6 Stage C).

Separate stages, each testable: seed discovery -> bounded graph search ->
transparent ranking -> explainable output. Reuses `graph.algorithms` traversal
primitives; adds budgets, relation filtering, hub/external penalties and
deterministic K-path enumeration that the old ranked[0..1] shortcut lacked.
"""
from __future__ import annotations
import time
from collections import deque

DEFAULT_RELATIONS = frozenset({
    "calls", "defines", "contains", "imports", "depends_on", "exports",
    "inherits", "implements", "uses", "references", "imports_from",
})
# Traversable but weak: real evidence of a step, not proof of the target.
WEAK_RELATIONS = frozenset({"calls_unresolved"})

# Configurable, inspectable ranking weights (reported per query; tuned on dev
# queries only, never on held-out).
PATH_WEIGHTS = {
    "length_penalty": 2.0,      # per hop
    "resolved_bonus": 1.5,      # per resolved-structural hop
    "unresolved_penalty": 4.0,  # per weak/unresolved hop
    "external_penalty": 5.0,    # per external-placeholder node on path
    "hub_penalty": 3.0,         # per high-degree intermediate node
    "hub_degree": 50,           # out+in degree threshold
    "query_agreement": 1.0,     # per path node matching a query term
    "scope_bonus": 1.0,         # per node sharing the seed repository
}

TIME_BUDGET_S = 2.0
MAX_EXPANDED = 2000
MAX_PATHS = 5
MAX_DEPTH = 4


def discover_seeds(store, identifiers: list[str], repo_scope: str | None = None,
                   limit: int = 3, lexical_fallback: bool = True) -> dict:
    """Resolve endpoint candidates per identifier, independently.

    Order: exact qualified name -> exact label -> unique partial match ->
    lexical fallback (marked). Ambiguity is recorded, never silently collapsed.
    Returns {"endpoints": [{identifier, candidates, ambiguous,
    lexical_fallback}], ...}.
    """
    endpoints = []
    for ident in identifiers:
        key = (ident or "").strip().strip("`'\"")
        cands: list = []
        from_lexical = False
        if key:
            for n in store.search_symbols(key, limit * 2, partial=False):
                if _in_scope(n, repo_scope) and _is_real(n):
                    cands.append(n)
            if not cands:
                seen = set()
                for n in store.search_symbols(key, limit * 2, partial=True):
                    if n.id in seen or not _in_scope(n, repo_scope):
                        continue
                    seen.add(n.id)
                    cands.append(n)
                    if len(cands) >= limit:
                        break
            if not cands and lexical_fallback:
                for n in store.search_lexical(key, limit):
                    if n.id in seen or not _in_scope(n, repo_scope):
                        continue
                    seen.add(n.id)
                    cands.append(n)
                    if len(cands) >= limit:
                        break
                from_lexical = bool(cands)
        real = [n for n in cands if _is_real(n)]
        endpoints.append({"identifier": ident,
                          "candidates": (real or cands)[:limit],
                          "ambiguous": len(real or cands) > 1,
                          "lexical_fallback": from_lexical})
    return {"endpoints": endpoints}


def _is_real(n) -> bool:
    return getattr(n, "type", "") != "external_symbol"


def _in_scope(n, repo_scope: str | None) -> bool:
    if not repo_scope:
        return True
    return getattr(n, "repository_id", "") == repo_scope


def find_paths(store, sources: list[str], targets: list[str],
               max_depth: int = MAX_DEPTH, max_expanded: int = MAX_EXPANDED,
               max_paths: int = MAX_PATHS, relations=None,
               repo_scope: str | None = None,
               time_budget_s: float = TIME_BUDGET_S) -> dict:
    """Bounded multi-source BFS enumerating simple paths to any target.

    Deterministic: neighbors visited in (relation, target) order; cycles and
    repeats excluded per path; stops on budget/path-count/depth limits and says
    which bound fired. Directions are preserved; every hop records its edge.
    """
    t0 = time.time()
    allowed = set(relations) if relations else set(DEFAULT_RELATIONS)
    target_set = set(targets)
    # adjacency once (bounded by store limits, documented)
    adj: dict[str, list] = {}
    for e in store.all_edges(limit=200000):
        if e["relation"] not in allowed and e["relation"] not in WEAK_RELATIONS:
            continue
        if repo_scope:
            s = store.get_node(e["source"])
            t = store.get_node(e["target"])
            if s is not None and getattr(s, "repository_id", "") != repo_scope:
                continue
            if t is not None and getattr(t, "repository_id", "") != repo_scope:
                continue
        adj.setdefault(e["source"], []).append(e)
    for v in adj.values():
        v.sort(key=lambda e: (e["relation"], e["target"], e["id"]))
    paths: list = []
    expanded = 0
    budget_hit: str | None = None
    queue: deque = deque()
    for s in sources:
        queue.append((s, [s], []))
    seen_paths = set()
    while queue:
        if len(paths) >= max_paths:
            budget_hit = budget_hit or "max_paths"
            break
        if expanded >= max_expanded:
            budget_hit = "max_expanded"
            break
        if time.time() - t0 > time_budget_s:
            budget_hit = "time_budget"
            break
        node, npath, epath = queue.popleft()
        if len(npath) - 1 >= max_depth:
            continue
        for e in adj.get(node, []):
            nxt = e["target"]
            if nxt in npath:
                continue  # simple paths only: deterministic cycle guard
            npath2 = npath + [nxt]
            epath2 = epath + [e]
            if nxt in target_set:
                key = tuple(npath2)
                if key not in seen_paths:
                    seen_paths.add(key)
                    paths.append({"nodes": npath2, "edges": epath2})
                    if len(paths) >= max_paths:
                        budget_hit = "max_paths"
                        break
                continue
            expanded += 1
            queue.append((nxt, npath2, epath2))
    return {"paths": paths, "expanded": expanded,
            "budget_hit": budget_hit, "sources": list(sources),
            "targets": list(targets), "max_depth": max_depth}


def rank_paths(store, paths: list[dict], query_terms: set[str] | None = None,
               seed_repo: str | None = None, weights: dict | None = None) -> list[dict]:
    """Transparent scoring. Higher is better. Every feature is reported so the
    selection reason is inspectable; shortest is NOT assumed most relevant."""
    W = dict(PATH_WEIGHTS)
    if weights:
        W.update(weights)
    terms = {t.lower() for t in (query_terms or set())}
    ranked = []
    for p in paths:
        nodes, edges = p["nodes"], p["edges"]
        feats = {"hops": len(edges), "resolved": 0, "unresolved": 0, "external": 0,
                 "hubs": 0, "term_hits": 0, "in_scope": 0, "ambiguous": 0}
        for e in edges:
            rel = e["relation"] if isinstance(e, dict) else e.relation
            if rel in WEAK_RELATIONS:
                feats["unresolved"] += 1
            else:
                feats["resolved"] += 1
        for nid in nodes:
            n = store.get_node(nid)
            if n is None:
                feats["ambiguous"] += 1
                continue
            if getattr(n, "type", "") == "external_symbol":
                feats["external"] += 1
            try:
                deg = len(store.neighbors(nid, direction="both", limit=W["hub_degree"] + 1))
            except Exception:
                deg = 0
            if deg > W["hub_degree"]:
                feats["hubs"] += 1
            if seed_repo and getattr(n, "repository_id", "") == seed_repo:
                feats["in_scope"] += 1
            if terms:
                import re
                toks = set(re.findall(r"[A-Za-z_][\w]*",
                                      f"{getattr(n, 'label', '')} {getattr(n, 'qualified_name', '')}".lower()))
                if toks & terms:
                    feats["term_hits"] += 1
        score = (feats["resolved"] * W["resolved_bonus"]
                 + feats["term_hits"] * W["query_agreement"]
                 + feats["in_scope"] * W["scope_bonus"]
                 - feats["hops"] * W["length_penalty"]
                 - feats["unresolved"] * W["unresolved_penalty"]
                 - feats["external"] * W["external_penalty"]
                 - feats["hubs"] * W["hub_penalty"])
        ranked.append({**p, "features": feats, "score": round(score, 3),
                       "reason": _reason(feats, W)})
    ranked.sort(key=lambda r: (-r["score"], len(r["nodes"]), r["nodes"]))
    return ranked


def _reason(feats: dict, W: dict) -> str:
    bits = [f"{feats['hops']} hops"]
    if feats["unresolved"]:
        bits.append(f"{feats['unresolved']} unresolved steps")
    if feats["external"]:
        bits.append(f"{feats['external']} external placeholders")
    if feats["hubs"]:
        bits.append(f"{feats['hubs']} hub nodes")
    if feats["term_hits"]:
        bits.append(f"{feats['term_hits']} query-term matches")
    return "; ".join(bits)


def explain(store, ranked: list[dict], limit_paths: int = 3) -> list[dict]:
    """Attach per-hop evidence, locations and uncertainty flags."""
    out = []
    for p in ranked[:limit_paths]:
        hops = []
        for nid, e in zip(p["nodes"], p["edges"]):
            n = store.get_node(nid)
            erel = e["relation"] if isinstance(e, dict) else e.relation
            eid = e["id"] if isinstance(e, dict) else e.id
            prov = []
            try:
                row = store.cx.execute("SELECT provenance FROM edges WHERE id=?",
                                       (eid,)).fetchone()
                import json
                prov = json.loads(row["provenance"] or "[]") if row else []
            except Exception:
                pass
            ev = []
            for pid in prov[:3]:
                try:
                    r = store.cx.execute("SELECT id, source_uri, quote, start_line,"
                                         " end_line FROM evidence WHERE id=?",
                                         (pid,)).fetchone()
                    if r:
                        ev.append(dict(r))
                except Exception:
                    pass
            hops.append({"node": nid,
                         "label": getattr(n, "label", nid) if n else nid,
                         "qualified": getattr(n, "qualified_name", "") if n else "",
                         "type": getattr(n, "type", "?") if n else "?",
                         "file": getattr(n, "file_path", "") if n else "",
                         "line": getattr(getattr(n, "location", None), "start_line", 0)
                         if n else 0,
                         "via": erel, "edge_id": eid, "evidence": ev,
                         "uncertain": erel in WEAK_RELATIONS
                         or (getattr(n, "type", "") == "external_symbol" if n else False)})
        last = store.get_node(p["nodes"][-1]) if p["nodes"] else None
        out.append({"nodes": p["nodes"], "score": p.get("score"),
                    "features": p.get("features"), "reason": p.get("reason"),
                    "end": getattr(last, "qualified_name", "") if last else "",
                    "hops": hops})
    return out
