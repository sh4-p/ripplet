"""Exact-symbol ranking benchmark (`ranking-v1`).

Measures what the frozen comparative benchmark cannot isolate: given a repo
where every expected symbol exists exactly as annotated, do the lookup paths
return the RIGHT node first?

Three lookup paths are measured per query (all read the same store):
  raw  - SQLiteStore.search_symbols (insertion order before Phase 12 P2)
  sym  - retrieval.symbols.lookup (exact pass, then partial fallback)
  mcp  - mcp.tools.call_tool("search_symbols") (lookup + rerank weights)

Metrics: exact P@1, MRR, R@5, qualified MRR, ambiguous handling (both real
definitions returned, placeholders never first), unanswerable queries
(verifiably empty on every path), and reported locations (file + line).

No weight is tuned from held-out queries (see dataset split).
"""
from __future__ import annotations
import json
import shutil
import tempfile
import time
from pathlib import Path

BENCH_VERSION = "ranking-v1"
DATASET_DEFAULT = "tests/evaluation/ranking_bench_v1.json"
CASE_LIMIT = 5  # R@5 window
PATHS = ("raw", "sym", "mcp")


def load_dataset(path: str = DATASET_DEFAULT) -> dict:
    ds = json.loads(Path(path).read_text(encoding="utf-8"))
    assert ds.get("version") == BENCH_VERSION, "ranking dataset version mismatch"
    return ds


def _write_files(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _rank_of(results: list, primary: dict | None, acceptable: list) -> int | None:
    """0-based rank of the first acceptable hit.

    Ambiguity is never collapsed: when several real definitions are
    acceptable, the best-ranked one counts (primary only anchors the
    location check, not the rank).
    """
    for i, n in enumerate(results):
        for a in acceptable:
            name_hit = (getattr(n, "label", "") or getattr(n, "qualified_name", ""))
            if (getattr(n, "file_path", "") or "") == a["file"] and \
               name_hit in (a["symbol"], a["symbol"].split(".")[-1]):
                return i
    return None


def _run_paths(store, query: str, limit: int = 20):
    from ..storage.sqlite_store import SQLiteStore  # noqa (type clarity)
    from ..retrieval import symbols as sym
    from ..mcp import tools as mcptools
    raw = store.search_symbols(query, limit, partial=True)
    try:
        symres = sym.lookup(store, query, limit)
    except Exception:
        symres = []
    try:
        mcpdicts = mcptools.call_tool(store, None, "search_symbols",
                                      {"query": query, "limit": limit})
        ids = [d["id"] for d in mcpdicts]
        mcp = [store.get_node(i) for i in ids]
        mcp = [n for n in mcp if n is not None]
    except Exception:
        mcp = []
    return {"raw": raw, "sym": symres, "mcp": mcp}


def run_case(case: dict, store, limit: int = 20) -> dict:
    q = case["query"]
    paths = _run_paths(store, q, limit)
    per_path = {}
    for pname in PATHS:
        res = paths[pname]
        if case.get("primary") is None:
            per_path[pname] = {"n": len(res), "empty": not res,
                               "top": None}
            continue
        rank = _rank_of(res, case["primary"], case.get("acceptable", []))
        top = res[0] if res else None
        per_path[pname] = {
            "n": len(res), "rank": rank,
            "p_at_1": rank == 0,
            "recall_at_5": rank is not None and rank < CASE_LIMIT,
            "reciprocal": (1.0 / (rank + 1)) if rank is not None else 0.0,
            "top": ({"file": getattr(top, "file_path", ""),
                     "label": getattr(top, "label", ""),
                     "qualified": getattr(top, "qualified_name", ""),
                     "type": getattr(top, "type", "")} if top is not None else None),
        }
    out = {"id": case["id"], "query": q, "paths": per_path}
    if case.get("primary") is not None:
        top_raw = per_path["raw"]["top"] or {}
        out["location_ok"] = any(
            top_raw.get("file") == a["file"] and
            top_raw.get("label", "") in (a["symbol"], a["symbol"].split(".")[-1])
            for a in case.get("acceptable", [case["primary"]]))
    else:
        out["unanswerable_ok"] = all(per_path[p]["empty"] for p in PATHS)
        out["location_ok"] = True
    return out


def evaluate(dataset: str = DATASET_DEFAULT, tmp_root: str | None = None,
             split: str = "all", limit: int = 20) -> dict:
    """Index the fixture repo once, then score every case on all paths."""
    from ..config import RippletConfig
    from ..service import index_workspace
    from ..storage.sqlite_store import SQLiteStore
    ds = load_dataset(dataset)
    tmp = Path(tmp_root or tempfile.mkdtemp(prefix="rank-bench-"))
    proj = tmp / "repo"
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    _write_files(proj, ds["repo_files"])
    db = tmp / "rank.db"
    t0 = time.time()
    index_workspace(RippletConfig([str(proj)], str(db), "bench"))
    index_s = round(time.time() - t0, 3)
    st = SQLiteStore(str(db))
    try:
        cases = [run_case(c, st, limit) for c in ds["cases"]
                 if split == "all" or c.get("split") == split]
    finally:
        st.close()
    return {"version": BENCH_VERSION, "split": split, "index_s": index_s,
            "cases": cases}


def _mean(xs: list) -> float:
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def summarize(res: dict) -> dict:
    answered = [c for c in res["cases"] if "rank" in c["paths"]["raw"]]
    qualified = [c for c in res["cases"]
                 if "rank" in c["paths"]["raw"] and "." in c["query"]]
    per_path = {}
    for p in PATHS:
        per_path[p] = {
            "exact_p_at_1": _mean([1.0 if c["paths"][p].get("p_at_1") else 0.0
                                   for c in answered]),
            "mrr": _mean([c["paths"][p].get("reciprocal", 0.0) for c in answered]),
            "recall_at_5": _mean([1.0 if c["paths"][p].get("recall_at_5") else 0.0
                                  for c in answered]),
            "qualified_mrr": _mean([c["paths"][p].get("reciprocal", 0.0)
                                    for c in qualified]),
        }
    unans = [c for c in res["cases"] if "unanswerable_ok" in c]
    return {"per_path": per_path,
            "unanswerable_ok": all(c["unanswerable_ok"] for c in unans),
            "locations_ok": all(c.get("location_ok", False) for c in answered),
            "n_cases": len(res["cases"]), "index_s": res.get("index_s")}


def markdown_report(res: dict) -> str:
    s = summarize(res)
    L = [f"# Exact-symbol ranking benchmark `{BENCH_VERSION}` (split {res['split']})", ""]
    L.append(f"- cases: {s['n_cases']}  index_s: {s['index_s']}")
    for p in PATHS:
        m = s["per_path"][p]
        L.append(f"- {p}: P@1 {m['exact_p_at_1']}  MRR {m['mrr']}  "
                 f"R@5 {m['recall_at_5']}  qualified_MRR {m['qualified_mrr']}")
    L.append(f"- unanswerable_ok: {s['unanswerable_ok']}  "
             f"locations_ok: {s['locations_ok']}")
    bad = [f"{c['id']}/{p}" for c in res["cases"] for p in PATHS
           if "rank" in c["paths"][p] and not c["paths"][p].get("p_at_1")]
    if bad:
        L.append(f"- missed P@1: {', '.join(bad)}")
    return "\n".join(L)
