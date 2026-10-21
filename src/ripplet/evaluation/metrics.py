from __future__ import annotations
def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if k<=0: return 0.0
    hit=len([x for x in ranked[:k] if x in relevant])
    return hit/min(k,len(ranked) or 1)
def recall_at_k(ranked, relevant, k):
    if not relevant: return 1.0
    return len([x for x in ranked[:k] if x in relevant])/len(relevant)
def mrr(ranked, relevant):
    for i,x in enumerate(ranked,1):
        if x in relevant: return 1.0/i
    return 0.0
def ndcg_at_k(ranked, relevant, k):
    import math
    dcg=sum(1.0/math.log2(i+2) for i,x in enumerate(ranked[:k]) if x in relevant)
    ideal=sum(1.0/math.log2(i+2) for i in range(min(k,len(relevant))))
    return dcg/ideal if ideal else 0.0
