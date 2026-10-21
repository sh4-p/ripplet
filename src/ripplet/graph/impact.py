from __future__ import annotations
from .algorithms import adjacency
from collections import deque
def analyze(symbols: list[str], edges, max_depth: int = 4) -> list[dict]:
    adj, radj = adjacency(edges)
    out=[]
    for s in symbols:
        seen={s}; q=deque([(s,0,[s])])
        while q:
            n,d,path=q.popleft()
            if d>0:
                out.append({"affected": n, "via": list(path), "depth": d, "direct": d==1, "confidence": "resolved" if d<=2 else "hypothetical"})
            if d>=max_depth: continue
            for src,rel,eid in radj.get(n,[]):
                if src not in seen:
                    seen.add(src); q.append((src,d+1,path+[src]))
    # also downstream (calls made by changed symbol)
    return sorted(out, key=lambda x: (x["depth"], x["affected"]))
def find_tests(nodes, edges, symbols: list[str], max_depth: int = 3) -> list[str]:
    adj, radj = adjacency(edges)
    tests={n.id if hasattr(n,"id") else n for n in nodes if (getattr(n,"type","") in ("test",) or "test" in (getattr(n,"file_path","") or "").lower() or "test" in (n.id if hasattr(n,"id") else "").lower())}
    found=set()
    import collections
    for s in symbols:
        seen={s}; q=collections.deque([(s,0)])
        while q:
            n,d=q.popleft()
            if n in tests: found.add(n)
            if d>=max_depth: continue
            for src,rel,eid in radj.get(n,[]):
                if src not in seen: seen.add(src); q.append((src,d+1))
    return sorted(found)


def find_tests_compact(nodes, edges, symbols: list[str], max_depth: int = 3,
                       limit: int = 200, offset: int = 0) -> dict:
    """Opt-in compact test discovery (Phase 22): group related tests by
    file instead of returning bare id lists that force per-id expansion
    calls. Reasons describe structural reachability only — never coverage.
    Nothing is silently omitted: totals cover the full result set and any
    truncation is explicit with a continuation offset."""
    import collections
    adj, radj = adjacency(edges)
    by_id = {}
    for n in nodes:
        nid = getattr(n, "id", n)
        by_id[nid] = n

    def _is_test(n) -> bool:
        return (getattr(n, "type", "") in ("test",) or
                "test" in (getattr(n, "file_path", "") or "").lower() or
                "test" in (getattr(n, "id", "") or "").lower())

    # hits: test_id -> {"seeds": {seed: {"depth": d, "relations": set}}}
    hits: dict[str, dict] = {}
    for s in symbols:
        seen = {s}
        q = collections.deque([(s, 0, [])])
        while q:
            n, d, rels = q.popleft()
            node = by_id.get(n)
            if node is not None and _is_test(node):
                h = hits.setdefault(n, {"seeds": {}})
                prev = h["seeds"].get(s)
                if prev is None or d < prev["depth"]:
                    h["seeds"][s] = {"depth": d,
                                     "relations": sorted(set(rels))}
            if d >= max_depth:
                continue
            for src, rel, _eid in radj.get(n, []):
                if src not in seen:
                    seen.add(src)
                    q.append((src, d + 1, rels + [rel]))
    files: dict[str, dict] = {}
    for tid, h in hits.items():
        node = by_id.get(tid)
        fp = (getattr(node, "file_path", "") or "") if node is not None else ""
        g = files.setdefault(fp, {"file": fp, "test_ids": [],
                                  "seeds": set(), "min_depth": None,
                                  "relations": set()})
        g["test_ids"].append(tid)
        for seed, info in h["seeds"].items():
            g["seeds"].add(seed)
            g["relations"].update(info["relations"])
            if g["min_depth"] is None or info["depth"] < g["min_depth"]:
                g["min_depth"] = info["depth"]
    ordered = sorted(files.values(), key=lambda g: (g["file"] or "\uffff"))
    total_files = len(ordered)
    total_nodes = len(hits)
    page = ordered[offset:offset + limit] if limit and limit > 0 else ordered[offset:]
    out_files = []
    for g in page:
        seeds = sorted(g["seeds"])
        rels = sorted(g["relations"])
        out_files.append({
            "file": g["file"],
            "test_ids": sorted(g["test_ids"]),
            "seeds": seeds,
            "min_depth": g["min_depth"],
            "relations": rels,
            "reason": ("related to " + ", ".join(seeds) +
                       " via " + (", ".join(rels) if rels else "graph reachability") +
                       " (depth %d)" % (g["min_depth"] or 0)),
        })
    complete = (offset + len(page)) >= total_files
    return {
        "mode": "compact-files-v1",
        "symbols": list(symbols),
        "test_files_total": total_files,
        "test_nodes_total": total_nodes,
        "test_files": out_files,
        "evidence_category": "static-dependency",
        "evidence_note": ("Structural reachability only; not verified "
                          "runtime coverage. A listed file is related to the "
                          "seed symbols, not proven to exercise any behavior."),
        "complete": complete,
        "offset": offset,
        "limit": limit,
        "continuation": None if complete else {
            "offset": offset + len(page),
            "hint": "repeat with offset to list remaining files"},
        "expansion": ("get_node <test-id>, get_neighbors --id <test-id>, or "
                      "expand_evidence --node_id <test-id> --level span for "
                      "full evidence on any listed id"),
    }
