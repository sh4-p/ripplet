"""Route-chain expansion for `scope=deps` queries (E-05).

Lexical ranking can strand on a handler/route seed (`parts`) while the true
sink (`inventory.move`) sits one resolved hop away
(route --handles_route--> handler --calls--> service). This module lifts
those sinks into the fact set with their path evidence — bounded,
deterministic, resolved-edges only, never guessing.

Legacy (unscoped) responses are untouched; the caller applies this only on
the explicit `scope=deps` path before scope budgeting, so totals and
truncation notes stay honest.
"""
from __future__ import annotations

BUDGET = 5


def expand_deps(store, res: dict, budget: int = BUDGET) -> dict:
    """Append service-sink facts reachable via route chains. Returns res."""
    try:
        facts = res.get("facts") or []
    except Exception:
        return res
    if not isinstance(facts, list) or not facts:
        return res
    have = set()
    for f in facts:
        try:
            if isinstance(f, dict) and f.get("id"):
                have.add(f["id"])
        except Exception:
            continue
    try:
        edges = store.all_edges()
    except Exception:
        return res
    handlers: dict[str, str] = {}  # handler_id -> route_id ("" if unseeded)
    for e in edges:
        try:
            if e["relation"] != "handles_route":
                continue
            s, t = e["source"], e["target"]  # route -> handler
            if t in have:
                handlers.setdefault(t, s if s in have else "")
            elif s in have:
                handlers.setdefault(t, s)
        except Exception:
            continue
    if not handlers:
        return res
    calls: dict[str, list[str]] = {}
    for e in edges:
        try:
            if e["relation"] == "calls" and e["source"] in handlers:
                calls.setdefault(e["source"], []).append(e["target"])
        except Exception:
            continue
    added: list[dict] = []
    paths: list[list[str]] = []
    try:
        gpaths = res.get("graph_paths") or []
    except Exception:
        gpaths = []
    for h in sorted(handlers):
        for svc in sorted(set(calls.get(h, []))):
            if len(added) >= budget:
                break
            if svc in have or any(isinstance(a, dict) and a.get("id") == svc
                                 for a in added):
                continue
            try:
                n = store.get_node(svc)
            except Exception:
                n = None
            if n is None or getattr(n, "type", "") not in ("function", "method"):
                continue
            loc = getattr(n, "location", None)
            route = handlers[h]
            path = ([route] if route else []) + [h, svc]
            added.append({"id": n.id, "type": n.type, "label": n.label,
                          "qualified_name": n.qualified_name,
                          "file": n.file_path,
                          "line": loc.start_line if hasattr(loc, "start_line") else 1,
                          "via": "route-chain",
                          "via_path": path})
            paths.append(path)
            have.add(svc)
        if len(added) >= budget:
            break
    if not added:
        return res
    try:
        res["facts"] = list(facts) + added
        res["graph_paths"] = list(gpaths) + paths
        meta = res.get("retrieval_metadata")
        if isinstance(meta, dict):
            meta["route_chain"] = {"added": len(added), "budget": budget,
                                   "budget_hit": len(added) >= budget}
        if len(added) >= budget:
            lims = res.get("limitations")
            if isinstance(lims, list):
                lims.append(f"route-chain expansion stopped at budget: {budget}")
    except Exception:
        pass
    return res
