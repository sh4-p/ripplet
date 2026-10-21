"""`ripplet ui`: a local, read-only web interface over the index.

Standard library only (http.server); the page is static files in
`ui/static`. The graph is loaded into memory once and reloaded when the
database file changes (an `ripplet update` while the UI is open shows
up on the next request). Nothing here writes to the store.

Endpoints (JSON):
  /api/meta                      counts, repos, languages, relation kinds
  /api/search?q=&limit=          symbols ranked by name match then degree
  /api/node?id=                  one symbol: location, source, callers,
                                 callees, structure, related tests
  /api/neighborhood?id=&depth=   ego graph (both directions, capped)
  /api/map?level=                directory/file dependency map
  /api/impact?id=&depth=         reverse dependents by depth + tests
  /api/path?from=&to=            shortest dependency path (undirected hops)
  /api/http                      cross-language HTTP links
  /api/changes?mode=working|diff&base=
                                 changed symbols (git) + their impact
  /api/file?path=                a file's symbols and source
  /api/tree                      directory tree with symbol counts
  /api/rename?id=&to=            dry-run rename: lines it changes, sites to check
  /api/routes                    HTTP routes and their handlers
  /api/tables[?id=]              database tables, their columns and users
  /api/health                    per-language index coverage, freshness, agent setup
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import webbrowser
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC = Path(__file__).parent / "static"
SYMBOL_TYPES = ("function", "method", "class", "interface", "struct", "type", "trait",
                "enum", "test", "object", "route", "constant", "variable", "record")
DEP_RELS = ("calls", "instantiates", "calls_http", "inherits", "implements", "imports",
            "references", "uses", "depends_on", "tests", "dispatches")
STRUCT_RELS = ("defines", "contains")
CONTAINER_TYPES = ("class", "interface", "struct", "trait", "object", "record", "enum", "module")
MAX_EGO = 450


class Graph:
    """In-memory view of the store, rebuilt when the DB file changes."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.db = cfg.storage_path
        self.lock = threading.Lock()
        self.stamp = None
        self.load()

    def _db_stamp(self):
        st = []
        for suffix in ("", "-wal"):
            p = Path(self.db + suffix)
            if p.exists():
                s = p.stat()
                st.append((s.st_mtime_ns, s.st_size))
        return tuple(st)

    def fresh(self):
        if self._db_stamp() != self.stamp:
            with self.lock:
                if self._db_stamp() != self.stamp:
                    self.load()
        return self

    def load(self):
        cx = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, check_same_thread=False)
        cx.row_factory = sqlite3.Row
        self.stamp = self._db_stamp()
        nodes = {}
        for r in cx.execute("SELECT id, type, label, qualified_name, file_path, start_line, end_line, "
                            "language, repository_id, attributes FROM nodes"):
            nodes[r["id"]] = {
                "id": r["id"], "type": r["type"], "label": r["label"] or "",
                "qn": r["qualified_name"] or r["label"] or r["id"], "file": r["file_path"] or "",
                "line": r["start_line"] or 0, "end": r["end_line"] or r["start_line"] or 0,
                "lang": r["language"] or "", "repo": r["repository_id"] or "",
                "attrs": r["attributes"] or "{}"}
        sup = set()
        try:
            sup = {r[0] for r in cx.execute("SELECT edge_id FROM superseded_edges")}
        except sqlite3.Error:
            pass
        out_e, in_e = defaultdict(list), defaultdict(list)
        rel_count = defaultdict(int)
        for r in cx.execute("SELECT id, source, target, relation, confidence_class, file_path, start_line, "
                            "attributes FROM edges"):
            if r["id"] in sup:
                continue
            e = (r["source"], r["target"], r["relation"], r["confidence_class"] or "",
                 r["file_path"] or "", r["start_line"] or 0, r["attributes"] or "")
            out_e[r["source"]].append(e)
            in_e[r["target"]].append(e)
            rel_count[r["relation"]] += 1
        self.snapshots = [dict(r) for r in cx.execute(
            "SELECT * FROM snapshots ORDER BY created_at DESC LIMIT 20")] if _has_table(cx, "snapshots") else []
        cx.close()
        self.nodes, self.out_e, self.in_e, self.rel_count = nodes, out_e, in_e, dict(rel_count)
        self.parent = {}
        for s, lst in out_e.items():
            for e in lst:
                if e[2] in STRUCT_RELS:
                    self.parent.setdefault(e[1], s)
        self.degree = {nid: sum(1 for e in out_e.get(nid, ()) if e[2] in DEP_RELS)
                       + sum(1 for e in in_e.get(nid, ()) if e[2] in DEP_RELS) for nid in nodes}

    # ------------------------------------------------------------- helpers
    def brief(self, nid):
        n = self.nodes.get(nid)
        if n is None:
            return {"id": nid, "type": "external", "label": nid.split(":")[-1], "qn": nid,
                    "file": "", "line": 0, "lang": "", "repo": "", "external": True}
        b = {k: n[k] for k in ("id", "type", "label", "qn", "file", "line", "lang", "repo")}
        b["external"] = n["type"] in ("external_symbol", "external") or nid.startswith("ext:")
        b["degree"] = self.degree.get(nid, 0)
        return b

    def is_symbol(self, nid):
        n = self.nodes.get(nid)
        return n is not None and n["type"] in SYMBOL_TYPES

    def source(self, n, max_lines=400):
        if not n["file"] or not n["line"]:
            return []
        for root in self.cfg.workspace_roots or ["."]:
            p = Path(root) / n["file"]
            if p.is_file():
                try:
                    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    return []
                a = max(1, n["line"])
                b = max(a, n["end"] or a)
                b = min(b, a + max_lines - 1, len(lines))
                return [{"n": i, "t": lines[i - 1]} for i in range(a, b + 1)]
        return []

    def line_text(self, rel, ln):
        """One source line (stripped, capped), cached per file."""
        cache = self.__dict__.setdefault("_lines", {})
        if rel not in cache:
            if len(cache) > 400:
                cache.clear()
            t = self.file_text(rel)
            cache[rel] = t.splitlines() if t is not None else []
        lines = cache[rel]
        return lines[ln - 1].strip()[:200] if 0 < ln <= len(lines) else ""

    def file_text(self, rel):
        for root in self.cfg.workspace_roots or ["."]:
            p = Path(root) / rel
            if p.is_file():
                try:
                    return p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
        return None


def _has_table(cx, name):
    return cx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _edge_json(g, e, other_side, code=False):
    s, t, rel, conf, f, ln, attrs = e
    other = t if other_side == "target" else s
    d = {"rel": rel, "conf": conf, "file": f, "line": ln, "node": g.brief(other)}
    try:
        a = json.loads(attrs) if attrs else {}
    except ValueError:
        a = {}
    if rel == "calls_http":
        d["http"] = f"{a.get('http_method', '')} {a.get('url', '')}".strip()
    occ = sorted({o.get("line") for o in (a.get("occurrences") or []) if isinstance(o, dict) and o.get("line")})
    if len(occ) > 1:
        d["lines"] = occ                    # every call line of this caller
        d["line"] = occ[0]
    if a.get("via"):
        d["how"] = a["via"]                 # the binder that proved (or inferred) it
    if code and f:
        d["code"] = g.line_text(f, d["line"])
    return d


# ------------------------------------------------------------------ API
def api_meta(g, q):
    types = defaultdict(int)
    langs = defaultdict(int)
    repos = defaultdict(int)
    for n in g.nodes.values():
        types[n["type"]] += 1
        if n["type"] in SYMBOL_TYPES:
            if n["lang"]:
                langs[n["lang"]] += 1
            repos[n["repo"]] += 1
    roots = list(g.cfg.workspace_roots or [])
    return {"project": getattr(g.cfg, "project_id", "") or (Path(roots[0]).name if roots else "ripplet"),
            "roots": roots, "nodes": len(g.nodes), "edges": sum(g.rel_count.values()),
            "types": dict(types), "relations": g.rel_count, "languages": dict(langs),
            "repos": dict(repos), "snapshots": g.snapshots[:5],
            "symbol_types": list(SYMBOL_TYPES), "dep_relations": list(DEP_RELS)}


def api_search(g, q):
    term = (q.get("q") or "").strip()
    lim = min(int(q.get("limit") or 30), 200)
    if not term:
        top = sorted((n for n in g.nodes.values() if n["type"] in SYMBOL_TYPES),
                     key=lambda n: -g.degree.get(n["id"], 0))[:lim]
        return {"results": [g.brief(n["id"]) for n in top]}
    t = term.lower()
    scored = []
    for n in g.nodes.values():
        if n["type"] not in SYMBOL_TYPES and n["type"] != "file":
            continue
        lab, qn = n["label"].lower(), n["qn"].lower()
        if lab == t:
            s = 0
        elif lab.startswith(t):
            s = 1
        elif t in lab:
            s = 2
        elif t in qn:
            s = 3
        elif n["type"] == "file" and t in n["file"].lower():
            s = 3
        else:
            continue
        scored.append((s, -g.degree.get(n["id"], 0), len(n["qn"]), n["id"]))
    scored.sort()
    return {"results": [g.brief(x[3]) for x in scored[:lim]], "total": len(scored)}


def api_node(g, q):
    nid = q.get("id") or ""
    n = g.nodes.get(nid)
    if n is None:
        return {"error": "not found", "node": g.brief(nid)}
    try:
        attrs = json.loads(n["attrs"])
    except ValueError:
        attrs = {}
    callers = [_edge_json(g, e, "source", code=True) for e in g.in_e.get(nid, ()) if e[2] in DEP_RELS]
    callees = [_edge_json(g, e, "target") for e in g.out_e.get(nid, ()) if e[2] in DEP_RELS]
    if n["type"] in CONTAINER_TYPES:
        # a class is used through its members: lift their outside callers/callees
        members = {e[1] for e in g.out_e.get(nid, ()) if e[2] in STRUCT_RELS}
        inside = members | {nid}
        for m in members:
            for e in g.in_e.get(m, ()):
                if e[2] in DEP_RELS and e[0] not in inside:
                    d = _edge_json(g, e, "source")
                    d["via"] = g.nodes.get(m, {}).get("label", "")
                    callers.append(d)
            for e in g.out_e.get(m, ()):
                if e[2] in DEP_RELS and e[1] not in inside:
                    d = _edge_json(g, e, "target")
                    d["via"] = g.nodes.get(m, {}).get("label", "")
                    callees.append(d)
    unresolved = sorted({(e[1].split(":")[-1]) for e in g.out_e.get(nid, ()) if e[2] == "calls_unresolved"})
    children = [g.brief(e[1]) for e in g.out_e.get(nid, ()) if e[2] in STRUCT_RELS]
    chain, cur = [], g.parent.get(nid)
    while cur and len(chain) < 8:
        chain.append(g.brief(cur))
        cur = g.parent.get(cur)
    seeds = [nid] + ([e[1] for e in g.out_e.get(nid, ()) if e[2] in STRUCT_RELS] if n["type"] in CONTAINER_TYPES else [])
    tests = _tests_for(g, seeds)
    return {"node": g.brief(nid), "end": n["end"], "attributes": attrs, "source": g.source(n),
            "callers": callers[:300], "callers_total": len(callers),
            "callees": callees[:300], "callees_total": len(callees),
            "unresolved": unresolved[:60], "children": children[:300], "parents": chain[::-1],
            "tests": tests[:50], "unverified": _unverified(g, n)}


def _unverified(g, n):
    """Possible callers the graph could not prove (calls of the same name on
    receivers of unknown type): what an agent is told to check."""
    if n["type"] not in ("function", "method"):
        return {"sites": [], "total": 0}
    from ..retrieval.uncertain import unverified_sites
    cx = sqlite3.connect(f"file:{g.db}?mode=ro", uri=True, check_same_thread=False)
    try:
        sites, total = unverified_sites(cx, [str(r) for r in (g.cfg.workspace_roots or ["."])],
                                        [n["id"]], n["label"], limit=60)
    except Exception:
        return {"sites": [], "total": 0}
    finally:
        cx.close()
    return {"sites": [{"file": f, "line": ln, "code": code, "receiver": r} for f, ln, code, r in sites],
            "total": total}


def _tests_for(g, seeds, depth=4):
    found, seen, dq = [], set(seeds), deque((s, 0) for s in seeds)
    while dq:
        x, d = dq.popleft()
        n = g.nodes.get(x)
        if n and x not in seeds and (n["type"] == "test" or re.search(r"(^|/)(tests?|spec|__tests__)/|[._-](test|spec)\.|Tests?\.", n["file"] or "")):
            found.append(g.brief(x))
            continue
        if d >= depth:
            continue
        for e in g.in_e.get(x, ()):
            if e[2] in DEP_RELS and e[0] not in seen:
                seen.add(e[0])
                dq.append((e[0], d + 1))
    return found


def _rels(q, default=DEP_RELS):
    r = q.get("rels")
    return tuple(x for x in r.split(",") if x) if r else default


def api_neighborhood(g, q):
    nid = q.get("id") or ""
    depth = max(1, min(int(q.get("depth") or 1), 4))
    rels = _rels(q)
    direction = q.get("dir") or "both"
    keep = {nid: 0}
    edges = {}
    dq = deque([(nid, 0)])
    n0 = g.nodes.get(nid)
    if n0 and n0["type"] in CONTAINER_TYPES:
        # a class's connections live on its members: show them, then expand from them
        for e in g.out_e.get(nid, ()):
            if e[2] in STRUCT_RELS and e[1] in g.nodes and g.nodes[e[1]]["type"] in SYMBOL_TYPES:
                keep[e[1]] = 1
                edges[(nid, e[1], "defines")] = {"s": nid, "t": e[1], "rel": "defines", "conf": "", "w": 1}
                dq.append((e[1], 0))
    while dq and len(keep) < MAX_EGO:
        x, d = dq.popleft()
        if d >= depth:
            continue
        lists = []
        if direction in ("both", "out"):
            lists.append(("out", g.out_e.get(x, ())))
        if direction in ("both", "in"):
            lists.append(("in", g.in_e.get(x, ())))
        for side, lst in lists:
            for e in sorted(lst, key=lambda e: -g.degree.get(e[1] if side == "out" else e[0], 0)):
                if e[2] not in rels:
                    continue
                other = e[1] if side == "out" else e[0]
                if other not in g.nodes and not q.get("externals"):
                    continue
                key = (e[0], e[1], e[2])
                edges.setdefault(key, {"s": e[0], "t": e[1], "rel": e[2], "conf": e[3], "w": 0})
                edges[key]["w"] += 1
                if other not in keep:
                    if len(keep) >= MAX_EGO:
                        break
                    keep[other] = d + 1
                    dq.append((other, d + 1))
    nodes = []
    for k, d in keep.items():
        b = g.brief(k)
        b["hop"] = d
        nodes.append(b)
    es = [e for e in edges.values() if e["s"] in keep and e["t"] in keep]
    return {"center": nid, "nodes": nodes, "edges": es, "truncated": len(keep) >= MAX_EGO}


def _dir_of(path, level):
    parts = path.replace("\\", "/").split("/")
    if level <= 0 or len(parts) <= 1:
        return path
    return "/".join(parts[:min(level, len(parts) - 1)]) or parts[0]


def api_map(g, q):
    """Dependency map aggregated to directories (level N) or files (level 0)."""
    rels = _rels(q, ("calls", "instantiates", "calls_http", "inherits", "implements"))
    if (q.get("level") or "auto") == "auto":
        # the shallowest folder depth that gives a readable map
        files = {n["file"] for n in g.nodes.values() if n["type"] in SYMBOL_TYPES and n["file"]}
        level = 0
        for lv in range(1, 9):
            groups = len({_dir_of(f, lv) for f in files})
            level = lv
            if groups >= 12:
                break
        if len({_dir_of(f, level) for f in files}) > 160:
            level = max(1, level - 1)
    else:
        level = int(q.get("level"))
    group = {}
    size = defaultdict(int)
    langs = defaultdict(lambda: defaultdict(int))
    for n in g.nodes.values():
        if n["type"] in SYMBOL_TYPES and n["file"]:
            k = n["file"] if level == 0 else _dir_of(n["file"], level)
            group[n["id"]] = k
            size[k] += 1
            if n["lang"]:
                langs[k][n["lang"]] += 1
    w = defaultdict(int)
    kinds = defaultdict(lambda: defaultdict(int))
    for s, lst in g.out_e.items():
        gs = group.get(s)
        if gs is None:
            continue
        for e in lst:
            if e[2] not in rels:
                continue
            gt = group.get(e[1])
            if gt is None or gt == gs:
                continue
            w[(gs, gt)] += 1
            kinds[(gs, gt)][e[2]] += 1
    limit = int(q.get("limit") or 400)
    ranked = sorted(size, key=lambda k: -size[k])[:limit]
    keepset = set(ranked)
    nodes = [{"id": "dir:" + k, "label": k.rsplit("/", 1)[-1] or k, "path": k, "type": "file" if level == 0 else "dir",
              "size": size[k], "lang": max(langs[k], key=langs[k].get) if langs[k] else ""} for k in ranked]
    edges = [{"s": "dir:" + a, "t": "dir:" + b, "w": c, "rel": max(kinds[(a, b)], key=kinds[(a, b)].get),
              "kinds": dict(kinds[(a, b)])}
             for (a, b), c in w.items() if a in keepset and b in keepset]
    return {"nodes": nodes, "edges": edges, "level": level, "groups_total": len(size)}


def _impact(g, seeds, depth):
    dist = {s: 0 for s in seeds}
    via = {}
    dq = deque((s, 0) for s in seeds)
    while dq:
        x, d = dq.popleft()
        if d >= depth:
            continue
        for e in g.in_e.get(x, ()):
            if e[2] not in DEP_RELS or e[0] in dist or e[0] not in g.nodes:
                continue
            src = e[0]
            # a member's dependents reach its owner through `defines`; skip
            # pure containment so "impact" means "depends on", not "contains"
            dist[src] = d + 1
            via[src] = (x, e[2], e[4], e[5])
            dq.append((src, d + 1))
    return dist, via


def api_impact(g, q):
    seeds = [s for s in (q.get("id") or "").split(",") if s]
    depth = max(1, min(int(q.get("depth") or 3), 6))
    dist, via = _impact(g, seeds, depth)
    nodes, edges = [], []
    for nid, d in dist.items():
        b = g.brief(nid)
        b["hop"] = d
        nodes.append(b)
        if nid in via:
            t, rel, f, ln = via[nid]
            edges.append({"s": nid, "t": t, "rel": rel, "conf": "", "w": 1, "site": f"{f}:{ln}" if f else "",
                          "file": f, "line": ln, "code": g.line_text(f, ln) if f and d <= 2 else ""})
    tests = _tests_for(g, seeds)
    by_depth = defaultdict(int)
    for d in dist.values():
        by_depth[d] += 1
    files = sorted({g.nodes[n]["file"] for n in dist if n in g.nodes and g.nodes[n]["file"]})
    return {"seeds": seeds, "nodes": nodes[:1500], "edges": edges[:3000], "by_depth": dict(by_depth),
            "files": files[:500], "files_total": len(files), "tests": tests[:200], "tests_total": len(tests)}


def api_path(g, q):
    a, b = q.get("from") or "", q.get("to") or ""
    if a not in g.nodes or b not in g.nodes:
        return {"error": "unknown endpoint"}
    prev = {a: None}
    dq = deque([a])
    while dq:
        x = dq.popleft()
        if x == b:
            break
        for side, lst in (("out", g.out_e.get(x, ())), ("in", g.in_e.get(x, ()))):
            for e in lst:
                if e[2] not in DEP_RELS:
                    continue
                y = e[1] if side == "out" else e[0]
                if y not in prev and y in g.nodes:
                    prev[y] = (x, e, side)
                    dq.append(y)
        if len(prev) > 200000:
            break
    if b not in prev:
        return {"found": False, "nodes": [], "edges": []}
    chain, x = [], b
    while prev[x] is not None:
        p, e, side = prev[x]
        chain.append({"s": e[0], "t": e[1], "rel": e[2], "conf": e[3], "w": 1, "site": f"{e[4]}:{e[5]}"})
        x = p
    ids = {a, b} | {c["s"] for c in chain} | {c["t"] for c in chain}
    return {"found": True, "nodes": [g.brief(i) for i in ids], "edges": chain[::-1]}


def api_http(g, q):
    nodes, edges = {}, []
    for s, lst in g.out_e.items():
        for e in lst:
            if e[2] != "calls_http":
                continue
            for x in (e[0], e[1]):
                nodes.setdefault(x, g.brief(x))
            d = {"s": e[0], "t": e[1], "rel": "calls_http", "conf": e[3], "w": 1, "site": f"{e[4]}:{e[5]}"}
            try:
                a = json.loads(e[6] or "{}")
                d["http"] = f"{a.get('http_method', '')} {a.get('url', '')}".strip()
                d["cross_repo"] = bool(a.get("cross_repo"))
            except ValueError:
                pass
            edges.append(d)
    return {"nodes": list(nodes.values()), "edges": edges}


def api_changes(g, q):
    from ..testplan import changes as ch
    mode = q.get("mode") or "working"
    depth = max(1, min(int(q.get("depth") or 3), 6))
    out = {"mode": mode, "repos": []}
    seeds = {}
    roots = list(g.cfg.workspace_roots or [])
    repo_ids = {}
    for n in g.nodes.values():
        if n["type"] == "file" and n["repo"]:
            repo_ids.setdefault(n["repo"], 0)
    for root in roots:
        if not ch.is_repo(root):
            out["repos"].append({"root": root, "git": False})
            continue
        try:
            from ..change.check import _changed_files
            changed = _changed_files(root, q.get("base") if mode == "diff" and q.get("base") else None)
        except ValueError as e:
            out["repos"].append({"root": root, "error": str(e)})
            continue
        rels = {c.rel for c in changed}
        rid = next((n["repo"] for n in g.nodes.values() if n["type"] == "file" and n["file"] in rels), None)
        if rid is None and repo_ids:
            rid = next(iter(repo_ids))
        mapped = ch.map_to_symbols(_ROStore(g), rid or "", changed) if rid else {"symbols": {}, "unmapped_files": [], "deleted_files": []}
        for sid, info in mapped["symbols"].items():
            seeds[sid] = info
        out["repos"].append({"root": root, "git": True, "files": [{"path": c.rel, "status": c.status} for c in changed],
                             "unmapped": mapped["unmapped_files"], "deleted": mapped["deleted_files"]})
    dist, via = _impact(g, list(seeds), depth)
    out["changed"] = [dict(g.brief(s), change=v.get("change"), status=v.get("status")) for s, v in seeds.items()]
    nodes = []
    for nid, d in dist.items():
        b = g.brief(nid)
        b["hop"] = d
        nodes.append(b)
    edges = [{"s": nid, "t": v[0], "rel": v[1], "conf": "", "w": 1, "site": f"{v[2]}:{v[3]}"} for nid, v in via.items()]
    out["impact"] = {"nodes": nodes[:1500], "edges": edges[:3000], "total": len(dist)}
    out["tests"] = _tests_for(g, list(seeds))[:200]
    return out


class _ROStore:
    """The tiny slice of the store interface map_to_symbols needs."""

    def __init__(self, g):
        cx = sqlite3.connect(f"file:{g.db}?mode=ro", uri=True, check_same_thread=False)
        cx.row_factory = sqlite3.Row
        self.cx = cx


def api_file(g, q):
    rel = q.get("path") or ""
    syms = sorted((g.brief(n["id"]) | {"end": n["end"]} for n in g.nodes.values()
                   if n["file"] == rel and n["type"] in SYMBOL_TYPES), key=lambda b: b["line"])
    text = g.file_text(rel)
    return {"path": rel, "symbols": syms, "source": None if text is None else text.splitlines()[:5000]}


def api_tree(g, q):
    counts = defaultdict(int)
    for n in g.nodes.values():
        if n["type"] in SYMBOL_TYPES and n["file"]:
            counts[n["file"].replace("\\", "/")] += 1
    root = {"name": "", "path": "", "children": {}, "count": 0, "file": False}
    for f, c in counts.items():
        cur = root
        cur["count"] += c
        parts = f.split("/")
        for i, p in enumerate(parts):
            cur = cur["children"].setdefault(p, {"name": p, "path": "/".join(parts[:i + 1]), "children": {},
                                                 "count": 0, "file": i == len(parts) - 1})
            cur["count"] += c

    def pack(n):
        kids = sorted(n["children"].values(), key=lambda k: (k["file"], k["name"].lower()))
        return {"name": n["name"], "path": n["path"], "count": n["count"], "file": n["file"],
                "children": [pack(k) for k in kids]}
    return pack(root)


def api_rename(g, q):
    """Dry-run rename_symbol: the lines it would change and the sites left to check."""
    from ..change.rename import rename_symbol
    n = g.nodes.get(q.get("id") or "")
    new = (q.get("to") or "").strip()
    if n is None or not new:
        return {"error": "id and to are required"}
    text = rename_symbol(_ROStore(g), g.cfg, n["qn"], new, apply=False)
    return {"symbol": g.brief(n["id"]), "to": new, "text": text}


def api_routes(g, q):
    """HTTP routes and the handler each one runs."""
    out = []
    for n in g.nodes.values():
        if n["type"] != "route":
            continue
        try:
            a = json.loads(n["attrs"])
        except ValueError:
            a = {}
        handlers = [g.brief(e[1]) for e in g.out_e.get(n["id"], ()) if e[2] == "handles_route"]
        callers = [_edge_json(g, e, "source") for e in g.in_e.get(n["id"], ()) if e[2] == "calls_http"]
        out.append({"id": n["id"], "method": a.get("http_method", ""), "path": a.get("full_path") or n["label"],
                    "file": n["file"], "line": n["line"], "handler": handlers[0] if handlers else None,
                    "handler_text": a.get("handler", ""), "callers": callers[:20]})
    out.sort(key=lambda r: (r["path"], r["method"]))
    return {"routes": out, "total": len(out), "unbound": sum(1 for r in out if r["handler"] is None)}


_SQL_OP = re.compile(r"\b(INSERT|UPDATE|DELETE|SELECT|CREATE|ALTER|DROP|REPLACE|MERGE|JOIN)\b", re.I)


def api_tables(g, q):
    """Database tables: their columns and the code that reads or writes them."""
    tid = q.get("id")
    if not tid:
        rows = []
        for t in g.nodes.values():
            if t["type"] != "table":
                continue
            refs = [e for e in g.in_e.get(t["id"], ()) if e[2] == "references"]
            cols = [e[1] for e in g.out_e.get(t["id"], ()) if e[2] in STRUCT_RELS]
            rows.append({"id": t["id"], "name": t["label"], "file": t["file"], "line": t["line"],
                         "columns": len(cols), "users": len({e[0] for e in refs})})
        rows.sort(key=lambda r: (-r["users"], r["name"]))
        return {"tables": rows, "total": len(rows)}
    t = g.nodes.get(tid)
    if t is None:
        return {"error": "not found"}
    users = []
    for e in g.in_e.get(tid, ()):
        if e[2] != "references":
            continue
        d = _edge_json(g, e, "source", code=True)
        m = _SQL_OP.search(d.get("code") or "")
        op = m.group(1).upper() if m else ""
        # a statement spanning lines: `FROM tasks t` / `JOIN tasks` is a read
        d["op"] = "SELECT" if op in ("JOIN", "") and re.search(r"(FROM|JOIN)", d.get("code") or "", re.I) else (op or "SQL")
        users.append(d)
    cols = []
    for e in g.out_e.get(tid, ()):
        if e[2] in STRUCT_RELS and e[1] in g.nodes:
            c = g.nodes[e[1]]
            cols.append({"id": c["id"], "name": c["label"],
                         "users": len({x[0] for x in g.in_e.get(c["id"], ()) if x[2] == "references"})})
    return {"table": g.brief(tid), "columns": cols, "users": users[:400], "users_total": len(users)}


def api_health(g, q):
    """How complete the index is, per language, and whether agents are set up to use it."""
    langs = defaultdict(lambda: {"files": 0, "partial": 0, "symbols": 0, "calls": 0, "inferred": 0,
                                 "unresolved": 0, "possible": 0, "partial_files": []})
    proj_names = {n["label"] for n in g.nodes.values() if n["type"] in ("function", "method") and len(n["label"]) > 2}
    for n in g.nodes.values():
        L = n["lang"]
        if not L:
            continue
        if n["type"] == "file":
            langs[L]["files"] += 1
            try:
                a = json.loads(n["attrs"])
            except ValueError:
                a = {}
            if (a.get("parser") or {}).get("status") == "partial":
                langs[L]["partial"] += 1
                if len(langs[L]["partial_files"]) < 15:
                    langs[L]["partial_files"].append(n["file"])
        elif n["type"] in SYMBOL_TYPES:
            langs[L]["symbols"] += 1
    for src, lst in g.out_e.items():
        n = g.nodes.get(src)
        if n is None or not n["lang"]:
            continue
        for e in lst:
            if e[2] == "calls":
                langs[n["lang"]]["calls"] += 1
                if e[3] == "inferred":
                    langs[n["lang"]]["inferred"] += 1
            elif e[2] == "calls_unresolved":
                langs[n["lang"]]["unresolved"] += 1
                # a project symbol's name: a possible project call the graph could
                # not prove (the rest are library / runtime calls)
                try:
                    a_ = json.loads(e[6]) if e[6] else {}
                except ValueError:
                    a_ = {}
                nm = a_.get("member") or a_.get("implicit_member") or                     str(a_.get("unresolved_call") or a_.get("base") or "").rsplit(".", 1)[-1]
                if nm in proj_names:
                    langs[n["lang"]]["possible"] += 1
    try:
        db_time = os.path.getmtime(g.db)
    except OSError:
        db_time = 0
    stale = []
    files = {n["file"] for n in g.nodes.values() if n["type"] == "file" and n["file"]}
    for root in g.cfg.workspace_roots or ["."]:
        for f in files:
            try:
                if (Path(root) / f).stat().st_mtime > db_time + 1:
                    stale.append(f)      # edited after the index was written
            except OSError:
                continue
    agents = []
    for root in g.cfg.workspace_roots or ["."]:
        r = Path(root)

        def has(rel, needle="ripplet"):
            try:
                return needle in (r / rel).read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                return False
        agents.append({"root": str(r), "mcp": has(".mcp.json"), "claude_rules": has("CLAUDE.md"),
                       "agents_rules": has("AGENTS.md"), "claude_hooks": has(".claude/settings.json", "agent_hooks"),
                       "strict": has(".claude/settings.json", "--strict"), "opencode": has("opencode.json")})
    out = []
    for L, d in sorted(langs.items(), key=lambda kv: -kv[1]["files"]):
        # of the calls that name a project symbol, the share the graph proved
        total = d["calls"] + d["possible"]
        out.append(dict(d, language=L, bound_share=round(d["calls"] / total, 3) if total else None,
                        library=d["unresolved"] - d["possible"]))
    return {"languages": out, "indexed_at": db_time, "stale_files": sorted(stale)[:50],
            "stale_total": len(stale), "agents": agents, "nodes": len(g.nodes),
            "edges": sum(len(v) for v in g.out_e.values())}


ROUTES = {"/api/meta": api_meta, "/api/search": api_search, "/api/node": api_node,
          "/api/neighborhood": api_neighborhood, "/api/map": api_map, "/api/impact": api_impact,
          "/api/path": api_path, "/api/http": api_http, "/api/changes": api_changes,
          "/api/file": api_file, "/api/tree": api_tree, "/api/rename": api_rename,
          "/api/routes": api_routes, "/api/tables": api_tables, "/api/health": api_health}
CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}


def make_handler(graph: Graph):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path.startswith("/api/"):
                fn = ROUTES.get(u.path)
                if fn is None:
                    return self._send(404, b'{"error":"no such endpoint"}', "application/json")
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                try:
                    data = fn(graph.fresh(), q)
                    code = 200
                except Exception as e:  # report, never crash the server
                    data, code = {"error": f"{type(e).__name__}: {e}"}, 500
                return self._send(code, json.dumps(data, default=str).encode("utf-8"), "application/json")
            rel = "index.html" if u.path in ("", "/") else u.path.lstrip("/")
            p = (STATIC / rel).resolve()
            if STATIC.resolve() not in p.parents or not p.is_file():
                return self._send(404, b"not found", "text/plain")
            self._send(200, p.read_bytes(), CTYPES.get(p.suffix, "application/octet-stream"))
    return H


def serve(cfg, host: str = "127.0.0.1", port: int = 7420, open_browser: bool = True):
    if not Path(cfg.storage_path).exists():
        raise SystemExit(f"no index at {cfg.storage_path}; run `ripplet index` first")
    graph = Graph(cfg)
    srv = ThreadingHTTPServer((host, port), make_handler(graph))
    url = f"http://{host}:{srv.server_address[1]}/"
    print(f"Ripplet UI: {url}  ({len(graph.nodes)} nodes)  Ctrl+C to stop")
    if open_browser and not os.environ.get("RIPPLET_NO_BROWSER"):
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
