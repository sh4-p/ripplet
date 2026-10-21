"""Reproducible retrieval benchmark (Phase 4 Stage 2).

Ground truth comes from the versioned dataset file (hand-annotated fixture
facts), never from engine output. All strategies run on the same repository
snapshots with identical limits; every reported number regenerates via
``ripplet benchmark``.
"""
from __future__ import annotations
import json
import math
import time
from pathlib import Path

STRATEGIES = ["exact", "lexical", "graph", "symbol", "hybrid", "hybrid_rerank",
              "semantic", "lexical_semantic", "hybrid_full"]


class MemoProvider:
    """Query-vector memoizer for benchmark runs: identical query text embeds
    once per (model, text) no matter how many strategies use it."""

    def __init__(self, inner):
        self._inner = inner
        self._cache: dict[str, list[float]] = {}
        self.hits = 0
        self.misses = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def provider_id(self):
        return self._inner.provider_id()

    def embed(self, texts, cancel=None, progress=None):
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text):
        key = f"{self._inner.config.model if hasattr(self._inner, 'config') else ''}\x00{text}"
        if key in self._cache:
            self.hits += 1
            return list(self._cache[key])
        self.misses += 1
        vec = self._inner.embed_query(text)
        self._cache[key] = list(vec)
        return list(vec)


def load_dataset(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def split_of(qid: str) -> str:
    """Deterministic dev/held-out split (held_out iff sha1(qid) % 4 == 0).

    Ranking weights were frozen before the held-out set existed; tune only on
    dev, report both, decide on held-out.
    """
    import hashlib
    h = int(hashlib.sha1(qid.encode()).hexdigest(), 16)
    return "held_out" if h % 4 == 0 else "dev"


def filter_split(queries: list[dict], split: str) -> list[dict]:
    if split in (None, "", "all"):
        return list(queries)
    return [q for q in queries if split_of(q["qid"]) == split]


def paired_bootstrap(results: list[dict], strat_a: str, strat_b: str,
                     metric: str = "mrr", trials: int = 2000, seed: int = 1234) -> dict:
    """Paired bootstrap CI for (B − A) on identical queries. Small samples get
    honest intervals instead of point-compare claims."""
    import random
    diffs = []
    for r in results:
        a = r["strategies"].get(strat_a, {}).get(metric)
        b = r["strategies"].get(strat_b, {}).get(metric)
        if a is None or b is None:
            continue
        diffs.append(b - a)
    if not diffs:
        return {"n": 0, "mean": 0.0, "lo": 0.0, "hi": 0.0, "trials": trials}
    rng = random.Random(seed)
    means = []
    for _ in range(trials):
        sample = [rng.choice(diffs) for _ in diffs]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(0.025 * trials)]
    hi = means[min(trials - 1, int(0.975 * trials))]
    return {"n": len(diffs), "mean": round(sum(diffs) / len(diffs), 4),
            "lo": round(lo, 4), "hi": round(hi, 4), "trials": trials,
            "significant": (lo > 0) or (hi < 0)}


def _qualified(n) -> str:
    return getattr(n, "qualified_name", "") or getattr(n, "label", "")


def _semantic_list(store, cfg, provider, query: str, limit: int, diag: dict):
    from ..retrieval import semantic as sem
    model = getattr(cfg, "embedding_model", "") if cfg else ""
    if not model:
        diag["semantic"] = "off (no embedding_model)"
        return []
    try:
        if provider is None:
            from ..retrieval.embeddings import OllamaEmbeddingProvider, EmbeddingConfig
            provider = OllamaEmbeddingProvider(EmbeddingConfig(
                endpoint=getattr(cfg, "embedding_endpoint", "http://localhost:11434"),
                model=model,
                allow_remote=bool(getattr(cfg, "allow_remote_embeddings", False))))
        res = sem.search(store, provider, model, query, limit=limit)
    except Exception as e:
        diag["semantic"] = f"unavailable: {e}"
        return []
    if not res.get("available"):
        diag["semantic"] = res.get("reason", "unavailable")
        return []
    diag["semantic"] = {"model": model, "hits": len(res.get("results", []))}
    out = []
    for hit in res.get("results", [])[:limit]:
        n = store.get_node(hit["id"])
        if n is not None:
            out.append(n)
    return out


def run_strategy(store, cfg, name: str, query: str, limit: int, provider=None):
    """One strategy -> ranked Node list + diagnostics. Deterministic."""
    from ..retrieval import lexical, symbols, fusion, reranking, expansion
    from ..query.executor import execute
    t0 = time.time()
    diag: dict = {"strategy": name}
    if name == "exact":
        out = store.search_symbols(query.strip().strip("`'\""), limit, partial=False)
    elif name == "lexical":
        out = lexical.search(store, query[:80], limit)
    elif name == "graph":
        seeds = symbols.lookup(store, query.split()[0] if query.split() else query, 5)
        if not seeds:
            out = []
        else:
            ids = expansion.expand(store, [seeds[0].id], depth=2, limit=limit * 4)
            by_id = {n.id: n for n in store.all_nodes() if n.id in set(ids)}
            out = [seeds[0]] + sorted([n for i, n in by_id.items() if i != seeds[0].id],
                                      key=lambda n: n.qualified_name or n.label)[:limit - 1]
        diag["seed"] = _qualified(seeds[0]) if seeds else None
    elif name == "symbol":
        out = symbols.lookup(store, query, limit)
    elif name in ("hybrid", "hybrid_rerank"):
        from ..retrieval import terms as termex
        tex = termex.extract_terms(query)
        lists = [symbols.lookup(store, t, limit) for t in tex["identifiers"][:4]]
        lists.append(lexical.search(store, query[:80], limit))
        fused = fusion.rrf(lists)
        id2 = {}
        for lst in lists:
            for n in lst:
                id2[n.id] = n
        out = [id2[k] for k, _ in fused if k in id2][:limit]
        if name == "hybrid_rerank":
            from types import SimpleNamespace
            from ..query.executor import execute as _exec
            res = _exec(store, query, limit)
            out = [SimpleNamespace(id=f["id"], type=f["type"], label=f["label"],
                                   qualified_name=f["qualified_name"],
                                   file_path=f["file"], location=None,
                                   attributes={}, provenance_ids=[])
                   for f in res.get("facts", [])]
            diag["category"] = res.get("retrieval_metadata", {}).get("category")
        diag["candidates"] = len(fused)
    elif name == "semantic":
        # Same code path as product `query --mode semantic`, abstention included.
        from ..query.executor import execute as _exec
        from types import SimpleNamespace
        res = _exec(store, query, limit, cfg=cfg, provider=provider, mode="semantic")
        md = res.get("retrieval_metadata", {})
        diag["abstention"] = md.get("abstention")
        diag["semantic_scores"] = [f.get("score", 0.0) for f in res.get("facts", [])]
        diag["semantic_ids"] = [f.get("id") for f in res.get("facts", [])]
        out = [SimpleNamespace(id=f["id"], type=f["type"], label=f["label"],
                               qualified_name=f["qualified_name"],
                               file_path=f["file"], location=None,
                               attributes={}, provenance_ids=[])
               for f in res.get("facts", [])]
        diag["abstention"] = res.get("retrieval_metadata", {}).get("abstention")
    elif name == "lexical_semantic":
        lists = [lexical.search(store, query[:80], limit),
                 _semantic_list(store, cfg, provider, query, limit, diag)]
        fused = fusion.rrf(lists)
        id2 = {}
        for lst in lists:
            for n in lst:
                id2[n.id] = n
        out = [id2[k] for k, _ in fused if k in id2][:limit]
        diag["candidates"] = len(fused)
    elif name == "hybrid_full":
        from ..query.executor import execute as _exec
        from types import SimpleNamespace
        res = _exec(store, query, limit, cfg=cfg, provider=provider, mode="hybrid_full")
        out = [SimpleNamespace(id=f["id"], type=f["type"], label=f["label"],
                               qualified_name=f["qualified_name"],
                               file_path=f["file"], location=None,
                               attributes={}, provenance_ids=[])
               for f in res.get("facts", [])]
        diag["category"] = res.get("retrieval_metadata", {}).get("category")
        diag["semantic"] = res.get("retrieval_metadata", {}).get("semantic")
    else:
        raise ValueError(f"unknown strategy: {name}")
    diag["latency_s"] = round(time.time() - t0, 4)
    return out, diag


# ------------------------------------------------------------- metrics
def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if k <= 0:
        return 0.0
    return sum(1 for x in ranked[:k] if x in relevant) / k


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 1.0
    return sum(1 for x in ranked[:k] if x in relevant) / len(relevant)


def mrr(ranked: list[str], relevant: set[str]) -> float:
    for i, x in enumerate(ranked, 1):
        if x in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, x in enumerate(ranked[:k]) if x in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return dcg / ideal if ideal else 0.0


def _evidence_files(store, nodes) -> set[str]:
    files: set[str] = set()
    for n in nodes[:10]:
        for pid in (getattr(n, "provenance_ids", None) or [])[:3]:
            try:
                r = store.cx.execute("SELECT source_uri FROM evidence WHERE id=?",
                                     (pid,)).fetchone()
                if r:
                    files.add(r["source_uri"])
            except Exception:
                pass
    return files


def evaluate_query(store, cfg, record: dict, strategies: list[str], limit: int,
                   provider=None) -> dict:
    from ..mcp.tools import call_tool
    qid, text = record["qid"], record["text"]
    relevant = {e["qualified"] for e in record.get("expected", [])}
    out: dict = {"qid": qid, "category": record.get("category", ""),
                 "answerable": record.get("answerable", True), "strategies": {}}
    for name in strategies:
        ranked, diag = run_strategy(store, cfg, name, text, limit, provider=provider)
        # Deduplicate by qualified name (overloads share one): counting the
        # same answer three times would inflate recall above 1.0.
        seen_q, deduped = set(), []
        for n in ranked:
            q = _qualified(n)
            if q not in seen_q:
                seen_q.add(q)
                deduped.append(n)
        ranked = deduped
        # Re-resolve to stored nodes so evidence/location checks see full rows
        # even when a strategy returns lightweight projections.
        full = []
        for n in ranked:
            nid = getattr(n, "id", None)
            sn = store.get_node(nid) if nid else None
            full.append(sn if sn is not None else n)
        ranked = full
        quals = [_qualified(n) for n in ranked]
        lat = diag.pop("latency_s", 0.0)
        ev_files = sorted(_evidence_files(store, ranked))
        exp_files = {e.get("file", "") for e in record.get("expected", [])}
        entry = {
            "ranked": quals[:10],
            "p_at_1": round(precision_at_k(quals, relevant, 1), 4),
            "p_at_5": round(precision_at_k(quals, relevant, 5), 4),
            "p_at_10": round(precision_at_k(quals, relevant, 10), 4),
            "r_at_5": round(recall_at_k(quals, relevant, 5), 4),
            "r_at_10": round(recall_at_k(quals, relevant, 10), 4),
            "mrr": round(mrr(quals, relevant), 4),
            "ndcg_5": round(ndcg_at_k(quals, relevant, 5), 4),
            "ndcg_10": round(ndcg_at_k(quals, relevant, 10), 4),
            "exact_hit": bool(relevant) and (quals[:1] == sorted(relevant)[:1] or
                                             (quals and quals[0] in relevant)),
            "location_ok": None,
            "evidence_files": ev_files,
            "evidence_ok": bool(exp_files) and bool(set(ev_files) & exp_files),
            "abstained": len(quals) == 0,
            "latency_s": lat,
            "result_size": len(quals),
            "tokens_est": sum(max(1, len(str(f)) // 4) for f in quals[:10]),
            "diag": diag,
            "r_at_10": round(recall_at_k(quals, relevant, 10), 4),
        }
        if relevant and quals:
            entry["location_ok"] = None  # filled below when a primary target exists
            prim = record["expected"][0]
            entry["location_ok"] = any(
                getattr(n, "file_path", "") == prim.get("file", "") and
                _qualified(n) == prim["qualified"] for n in ranked[:5])
        out["strategies"][name] = entry
    # path + impact checks are strategy-independent (resolved tools, not ranking)
    if "path" in record:
        p = record["path"]
        try:
            res = call_tool(store, cfg, "find_path",
                            {"source": p["from"], "target": p["to"]})
            got = res.get("path", []) if isinstance(res, dict) else []
            out["path"] = {"expected_reachable": p.get("reachable", True),
                           "got_path": len(got) > 0, "length": len(got),
                           "correct": bool(got) == bool(p.get("reachable", True))}
        except Exception as e:
            out["path"] = {"error": str(e)[:200], "correct": False}
    if "impact" in record:
        imp = record["impact"]
        try:
            res = call_tool(store, cfg, "analyze_change_impact",
                            {"symbols": imp["symbols"]})
            found = set()
            for f in res.get("findings", []):
                n = store.get_node(f.get("affected", ""))
                if n:
                    found.add(n.qualified_name)
            want = set(imp.get("affected", []))
            out["impact"] = {"expected": sorted(want), "found": sorted(found),
                             "correct": bool(want & found)}
        except Exception as e:
            out["impact"] = {"error": str(e)[:200], "correct": False}
    # abstention correctness
    out["abstention_correct"] = (not record.get("answerable", True)) == all(
        v["abstained"] for v in out["strategies"].values()) \
        if not record.get("answerable", True) else None
    # calibration row for abstention analysis (dev-tuned, held-assessed)
    sem = out["strategies"].get("semantic", {})
    semdiag = sem.get("diag", {}) or {}
    # Raw cosine scores (pre-policy) for calibration: the policy-filtered facts
    # would bias unanswerable queries to 0.0 and fake a separation.
    raw_scores: list[float] = []
    try:
        from ..retrieval import semantic as _sem
        _model = getattr(cfg, "embedding_model", "") if cfg else ""
        if _model and provider is not None:
            _res = _sem.search(store, provider, _model, record["text"], limit=5)
            if _res.get("available"):
                raw_scores = [h.get("score", 0.0) for h in _res.get("results", [])]
    except Exception:
        pass
    if raw_scores or not record.get("answerable", True):
        from ..retrieval import terms as _termex
        tex = _termex.extract_terms(record["text"])
        idents = {i.lower() for i in tex["identifiers"]}
        sem_ids = set(semdiag.get("semantic_ids", []) or [])
        lex_ids = {n.id for n in store.search_lexical(record["text"][:80], 20)}
        ident_hit = False
        for nid in list(sem_ids)[:3]:
            n = store.get_node(nid)
            if n is not None and ((n.label or "").lower() in idents
                                  or (n.qualified_name or "").lower() in idents):
                ident_hit = True
                break
        out["calibration"] = {
            "answerable": record.get("answerable", True),
            "max_score": raw_scores[0] if raw_scores else 0.0,
            "overlap": bool(sem_ids & lex_ids),
            "ident": ident_hit,
            "abstention": semdiag.get("abstention"),
        }
    return out


def summarize(results: list[dict], strategies: list[str], index_info: dict | None = None) -> dict:
    summary: dict = {"strategies": {}, "categories": {}}
    for name in strategies:
        rows = [(r, r["strategies"][name]) for r in results if name in r["strategies"]]
        ans = [x for x in rows if x[0].get("answerable", True)]
        summary["strategies"][name] = {
            "n": len(rows),
            "mean_p1": round(sum(v["p_at_1"] for _, v in rows) / len(rows), 4) if rows else 0.0,
            "mean_p5": round(sum(v["p_at_5"] for _, v in rows) / len(rows), 4) if rows else 0.0,
            "mean_r5": round(sum(v["r_at_5"] for _, v in ans) / len(ans), 4) if ans else 1.0,
            "mean_r10": round(sum(v["r_at_10"] for _, v in ans) / len(ans), 4) if ans else 1.0,
            "mean_mrr": round(sum(v["mrr"] for _, v in rows) / len(rows), 4) if rows else 0.0,
            "mean_ndcg5": round(sum(v["ndcg_5"] for _, v in rows) / len(rows), 4) if rows else 0.0,
            "exact_acc": round(sum(1 for _, v in ans if v["exact_hit"]) / len(ans), 4) if ans else 1.0,
            "evidence_rate": round(sum(1 for _, v in ans if v["evidence_ok"]) / len(ans), 4) if ans else 1.0,
            "abstention_acc": _abst_acc(rows),
            "abstention": _abst_per_strategy(rows, name),
            "p50_latency_s": _pct(sorted(v["latency_s"] for _, v in rows), 50),
            "p95_latency_s": _pct(sorted(v["latency_s"] for _, v in rows), 95),
        }
    if index_info:
        summary["index"] = index_info
    cats: dict[str, list] = {}
    for r in results:
        cats.setdefault(r["category"], []).append(r)
    for cat, rs in cats.items():
        summary["categories"][cat] = {
            name: round(sum(x["strategies"][name]["mrr"] for x in rs) / len(rs), 4)
            for name in strategies}
    # path + impact rollups
    prows = [r["path"]["correct"] for r in results if "path" in r]
    summary["path_correct"] = f"{sum(prows)}/{len(prows)}" if prows else "n/a"
    irows = [r["impact"]["correct"] for r in results if "impact" in r]
    summary["impact_correct"] = f"{sum(irows)}/{len(irows)}" if irows else "n/a"
    return summary


def _abst_acc(rows) -> float | str:
    # Unanimous abstention across every evaluated strategy. Note: pure cosine
    # semantic search has no relevance threshold and (by design) never
    # abstains, so any run including semantic strategies scores 0 here — see
    # per-strategy `abstention` for the informative breakdown.
    unans = [r for r, _ in rows if not r.get("answerable", True)]
    if not unans:
        return "n/a"
    ok = 0
    for r in unans:
        if all(r["strategies"][n]["abstained"] for n in r["strategies"]):
            ok += 1
    return round(ok / len(unans), 4)


def _abst_per_strategy(rows, name) -> float | str:
    unans = [(r, r["strategies"][name]) for r, _ in rows
             if not r.get("answerable", True) and name in r["strategies"]]
    if not unans:
        return "n/a"
    return round(sum(1 for _, v in unans if v["abstained"]) / len(unans), 4)


def _pct(xs: list[float], p: int) -> float:
    if not xs:
        return 0.0
    i = min(len(xs) - 1, int(len(xs) * p / 100))
    return round(xs[i], 4)


def markdown_report(summary: dict, dataset_version: str) -> str:
    L = [f"# Retrieval benchmark (dataset v{dataset_version})", "",
         "Per-category MRR and overall means. Higher is better; abstention/path/impact "
         "are correctness fractions.", "",
         "## Overall (mean)", ""]
    L.append("| strategy | P@1 | P@5 | R@5 | R@10 | MRR | NDCG@5 | exact | evid | abst | p50(s) | p95(s) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, s in summary["strategies"].items():
        L.append(f"| {name} | {s['mean_p1']} | {s['mean_p5']} | {s['mean_r5']} | "
                 f"{s.get('mean_r10', '?')} | {s['mean_mrr']} | {s['mean_ndcg5']} | "
                 f"{s['exact_acc']} | {s.get('evidence_rate', '?')} | "
                 f"{s['abstention']} | {s['p50_latency_s']} | {s['p95_latency_s']} |")
    if summary.get("index"):
        L += ["", f"index: {summary['index']}"]
    L += ["", f"path_correct: {summary['path_correct']} | impact_correct: {summary['impact_correct']}",
          "", "## Per-category MRR", ""]
    names = list(summary["strategies"])
    L.append("| category | " + " | ".join(names) + " |")
    L.append("|---|" + "---|" * len(names))
    for cat, row in summary["categories"].items():
        L.append("| " + cat + " | " + " | ".join(str(row[n]) for n in names) + " |")
    return "\n".join(L) + "\n"
