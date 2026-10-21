from __future__ import annotations
def expand(store, seed_ids: list[str], depth: int = 1, limit: int = 100) -> list[str]:
    seen=set(seed_ids); frontier=list(seed_ids)
    for _ in range(depth):
        nxt=[]
        for nid in frontier:
            for e in store.neighbors(nid, direction="both", limit=50):
                for t in (e["source"], e["target"]):
                    if t not in seen and len(seen)<limit:
                        seen.add(t); nxt.append(t)
        frontier=nxt
    return list(seen)
