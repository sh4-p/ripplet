from __future__ import annotations
def rrf(lists: list[list], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str,float] = {}
    for lst in lists:
        for rank, item in enumerate(lst, start=1):
            key = item.id if hasattr(item,"id") else (item.get("id") if isinstance(item,dict) else str(item))
            scores[key] = scores.get(key,0)+1.0/(k+rank)
    return sorted(scores.items(), key=lambda x: -x[1])
def fuse_with_boost(rrf_scores, boost: dict[str,float] | None = None):
    if boost:
        return sorted(((k, v+boost.get(k,0)) for k,v in rrf_scores), key=lambda x: -x[1])
    return rrf_scores
