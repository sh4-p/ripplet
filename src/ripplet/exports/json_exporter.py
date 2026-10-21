from __future__ import annotations
import json
def export(store, path: str):
    nodes=[n.to_dict() for n in store.all_nodes()]
    edges=store.all_edges()
    with open(path,"w",encoding="utf-8") as f: json.dump({"nodes":nodes,"edges":edges},f,indent=2)
    return path
def subgraph(store, seed: str, depth: int = 2) -> dict:
    from ..graph.algorithms import bfs
    edges_o=store.all_edges()
    import types
    eo=[type("E",(),e)() for e in edges_o]
    # adapt: bfs expects .source/.target
    seen,_=bfs(seed, eo, max_depth=depth, direction="both")
    keep=set(seen)
    nodes=[n.to_dict() for n in store.all_nodes() if n.id in keep]
    ed=[e for e in edges_o if e["source"] in keep and e["target"] in keep]
    return {"nodes":nodes,"edges":ed}
