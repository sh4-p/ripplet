from __future__ import annotations
def evidence_for(store, node_ids: list[str], limit: int = 20) -> list[dict]:
    out=[]
    for nid in node_ids[:limit]:
        n=store.get_node(nid)
        if not n: continue
        for pid in (n.provenance_ids or [])[:3]:
            r=store.cx.execute("SELECT * FROM evidence WHERE id=?",(pid,)).fetchone()
            if r: out.append(dict(r))
    return out
def freshness(store) -> dict:
    try:
        n,e=store.count()
        r=store.cx.execute("SELECT MAX(updated_at) m FROM files").fetchone()
        return {"nodes":n,"edges":e,"last_index":r["m"] if r and r["m"] else None}
    except Exception: return {}
