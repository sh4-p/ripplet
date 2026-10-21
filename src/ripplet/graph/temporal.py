from __future__ import annotations
import time, json, hashlib
def snapshot_id(nodes, edges) -> str:
    h=hashlib.sha256()
    for n in sorted([n.id if hasattr(n,"id") else n for n in nodes]): h.update(n.encode())
    for e in sorted([e.id if hasattr(e,"id") else e for e in edges]): h.update(e.encode())
    return h.hexdigest()[:16]
def diff(old_nodes, old_edges, new_nodes, new_edges) -> dict:
    on={n.id if hasattr(n,"id") else n for n in old_nodes}; nn={n.id if hasattr(n,"id") else n for n in new_nodes}
    oe={e.id if hasattr(e,"id") else e for e in old_edges}; ne={e.id if hasattr(e,"id") else e for e in new_edges}
    return {"added_nodes": sorted(nn-on), "removed_nodes": sorted(on-nn), "added_edges": sorted(ne-oe), "removed_edges": sorted(oe-ne), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
