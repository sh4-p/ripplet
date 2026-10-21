from __future__ import annotations
from collections import deque, defaultdict
def adjacency(edges, directed=True):
    adj=defaultdict(list); radj=defaultdict(list)
    for e in edges:
        adj[e.source].append((e.target,e.relation,e.id))
        radj[e.target].append((e.source,e.relation,e.id))
        if not directed: adj[e.target].append((e.source,e.relation,e.id))
    return adj, radj
def bfs(start, edges, max_depth=4, relations=None, direction="out"):
    adj, radj = adjacency(edges)
    seen={start:0}; q=deque([(start,0,[start])]); order=[start]
    while q:
        n,d,path = q.popleft()
        if d>=max_depth: continue
        nbrs = adj[n] if direction in ("out","both") else []
        if direction=="both": nbrs = nbrs + radj[n]
        if direction=="in": nbrs = radj[n]
        for t,rel,eid in nbrs:
            if relations and rel not in relations: continue
            if t not in seen:
                seen[t]=d+1; order.append(t); q.append((t,d+1,path+[t]))
    return seen, order
def shortest_path(src, dst, edges, relations=None, max_depth=6):
    adj,_ = adjacency(edges)
    q=deque([(src,[src])]); seen={src}
    while q:
        n,path=q.popleft()
        if n==dst: return path
        if len(path)-1>=max_depth: continue
        for t,rel,eid in adj.get(n,[]):
            if relations and rel not in relations: continue
            if t not in seen:
                seen.add(t); q.append((t,path+[t]))
    return []
def validate_path(path, edges) -> dict:
    """Consistency check: every consecutive pair must share a correctly
    directed edge. Never fabricates paths — reports the first broken link."""
    if not path:
        return {"valid": False, "reason": "empty path"}
    if len(path) == 1:
        return {"valid": True, "hops": []}
    pairs = {}
    for e in edges:
        s = e["source"] if isinstance(e, dict) else e.source
        t = e["target"] if isinstance(e, dict) else e.target
        r = e["relation"] if isinstance(e, dict) else e.relation
        pairs.setdefault((s, t), []).append(r)
    hops = []
    for a, b in zip(path, path[1:]):
        rels = pairs.get((a, b))
        if not rels:
            return {"valid": False, "reason": f"no directed edge {a[:24]} -> {b[:24]}",
                    "hops": hops, "broken_at": [a, b]}
        hops.append({"from": a, "to": b, "via": sorted(set(rels))})
    return {"valid": True, "hops": hops}


def strongly_connected(nodes, edges):
    index={}; low={}; on=[]; on_set=set(); comps=[]; counter=[0]
    adj,_ = adjacency(edges)
    import sys
    sys.setrecursionlimit(10000)
    def visit(v):
        index[v]=low[v]=counter[0]; counter[0]+=1; on.append(v); on_set.add(v)
        for t,_,_ in adj.get(v,[]):
            if t not in index:
                visit(t); low[v]=min(low[v],low[t])
            elif t in on_set: low[v]=min(low[v],index[t])
        if low[v]==index[v]:
            comp=[]
            while True:
                w=on.pop(); on_set.discard(w); comp.append(w)
                if w==v: break
            if len(comp)>1: comps.append(comp)
    for n in nodes:
        nid = n.id if hasattr(n,"id") else n
        if nid not in index: visit(nid)
    return comps
def find_cycles(nodes, edges):
    return strongly_connected(nodes, edges)
def orphans(nodes, edges):
    touched=set()
    for e in edges: touched.add(e.source); touched.add(e.target)
    return [ (n.id if hasattr(n,"id") else n) for n in nodes if (n.id if hasattr(n,"id") else n) not in touched ]
def degrees(edges):
    from collections import Counter
    ind=Counter(); outd=Counter()
    for e in edges: outd[e.source]+=1; ind[e.target]+=1
    return dict(ind), dict(outd)
