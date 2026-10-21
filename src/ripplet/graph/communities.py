from __future__ import annotations
from collections import defaultdict, deque
def weakly_connected(nodes, edges):
    parent={}
    def find(x):
        while parent[x]!=x: parent[x]=parent[parent[x]]; x=parent[x]
        return x
    def union(a,b):
        ra,rb=find(a),find(b)
        if ra!=rb: parent[rb]=ra
    ids=[n.id if hasattr(n,"id") else n for n in nodes]
    for i in ids: parent[i]=i
    for e in edges:
        if e.source in parent and e.target in parent: union(e.source,e.target)
    groups=defaultdict(list)
    for i in ids: groups[find(i)].append(i)
    return list(groups.values())
