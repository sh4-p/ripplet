from __future__ import annotations
import time
from .planner import plan
from ..retrieval import lexical, symbols, fusion, reranking, terms as termex
from . import evidence as evm


def _scope_candidates(store, scope: str | None, limit: int):
    """Module/file/package nodes matching a dotted scope (architecture queries)."""
    if not scope:
        return []
    out = []
    for n in store.search_symbols(scope, limit, partial=True):
        if getattr(n, "type", "") in ("module", "file", "package", "directory"):
            out.append(n)
    return out


def _dereference(store, node, limit: int = 3):
    """Follow an external placeholder to the real symbol(s) it names.

    Import edges target `ext:` placeholders; the evidence the user asked for is
    the real definition. Resolution is strict (exact qualified or unique label)
    and never merges — ambiguous placeholders stay as they are.
    """
    if getattr(node, "type", "") != "external_symbol":
        return [node]
    name = (getattr(node, "qualified_name", "") or getattr(node, "label", "") or "")
    short = name.split(".")[-1].split(":")[-1]
    if name.startswith("import:"):
        return [node]
    exact = [n for n in store.search_symbols(name, limit, partial=False)
             if getattr(n, "type", "") != "external_symbol"]
    if len(exact) == 1:
        return exact
    if short:
        cands = [n for n in store.search_symbols(short, limit, partial=False)
                 if getattr(n, "type", "") != "external_symbol"]
        if len(cands) == 1:
            return cands
    return [node]


def _module_nodes(store, scope: str, limit: int = 5):
    mods = [n for n in store.search_symbols(scope, limit, partial=True)
            if getattr(n, "type", "") in ("module", "file")]
    # Prefer module nodes (imports/depends_on hang off modules, not files),
    # but keep the file node too: `contains` lives there.
    mods.sort(key=lambda n: 0 if getattr(n, "type", "") == "module" else 1)
    return mods


def _pick_importer(store, idents: list[str]):
    """For imports questions with several dotted spans, pick the span whose
    module actually imports things (the importer, not the importee)."""
    cands = []
    for ident in idents:
        for m in _module_nodes(store, ident, 3)[:1]:
            n_out = sum(1 for e in store.neighbors(
                m.id, direction="out", relations=["imports", "depends_on"],
                limit=5))
            cands.append((n_out, ident))
    if not cands:
        return idents[0] if idents else None
    cands.sort(key=lambda x: (-x[0], len(x[1])))
    return cands[0][1] if cands[0][0] > 0 else None


def _neighbor_candidates(store, scope: str | None, limit: int, idents: list[str] | None = None):
    """Typed neighborhood of the scope module (imports/exports queries).

    One hop plus the defines/exports children of reached modules, so an
    imported *symbol* (not just its module) is reachable. External import
    placeholders are dereferenced to real definitions when unambiguous.
    """
    if not scope and idents:
        scope = _pick_importer(store, [i for i in idents if "." in i]) or scope
    if not scope:
        return []
    mods = _module_nodes(store, scope)
    if not mods:
        return []
    seen, out = set(), []

    def _add(nid: str):
        if nid in seen or len(out) >= limit * 2:
            return
        seen.add(nid)
        n = store.get_node(nid)
        if n is not None:
            out.extend(_dereference(store, n))

    for m in mods[:2]:
        seen.add(m.id)
        out.append(m)
        for e in store.neighbors(m.id, direction="both",
                                 relations=["imports", "depends_on", "exports", "defines",
                                            "contains", "imports_from"],
                                 limit=limit * 2):
            _add(e["source"])
            _add(e["target"])
    # second hop: symbols defined/exported by reached modules
    for m in [n for n in list(out) if getattr(n, "type", "") == "module"][:6]:
        for e in store.neighbors(m.id, direction="out",
                                 relations=["defines", "exports"],
                                 limit=limit):
            _add(e["target"])
    return out[:limit]


def _semantic_candidates(store, cfg, provider, query: str, limit: int, diag: dict):
    """Opt-in semantic list. Returns [] with a recorded reason unless an
    embedding model is configured AND an index exists. Never raises."""
    from ..retrieval import semantic as sem
    if cfg is None or not getattr(cfg, "embedding_model", ""):
        diag["semantic"] = "off (no embedding_model configured)"
        return []
    model = cfg.embedding_model
    try:
        if provider is None:
            from ..retrieval.embeddings import OllamaEmbeddingProvider, EmbeddingConfig
            provider = OllamaEmbeddingProvider(EmbeddingConfig(
                endpoint=getattr(cfg, "embedding_endpoint", "http://localhost:11434"),
                model=model,
                allow_remote=bool(getattr(cfg, "allow_remote_embeddings", False))))
        res = sem.search(store, provider, model, query,
                         limit=getattr(cfg, "semantic_top_k", 20) or 20)
    except Exception as e:
        diag["semantic"] = f"unavailable: {e}"
        return []
    if not res.get("available"):
        diag["semantic"] = res.get("reason", "unavailable")
        return []
    diag["semantic"] = {"model": res.get("model"), "dim": res.get("dim"),
                        "stale": res.get("stale"), "hits": len(res.get("results", []))}
    out = []
    for hit in res.get("results", [])[:limit]:
        n = store.get_node(hit["id"])
        if n is not None:
            out.append(n)
    return out


def execute(store, query: str, limit: int = 20, cfg=None, provider=None,
            mode: str = "hybrid") -> dict:
    t0 = time.time()
    p = plan(query)
    cat = p["category"]
    tex = termex.extract_terms(query)
    idents, keywords, scope = tex["identifiers"], tex["keywords"], tex["scope"]
    lists = []
    for t in idents[:4]:
        lists.append(symbols.lookup(store, t, limit))
    if cat == "architecture_overview":
        lists.append(_scope_candidates(store, scope, limit))
        if keywords:
            # OR semantics: architecture jargon ("package", "structure") rarely
            # matches node text verbatim; any overlap is a candidate.
            lists.append(lexical.search(store, " OR ".join(keywords[:4]), limit))
    elif cat == "imports_exports":
        lists.append(_neighbor_candidates(store, scope, limit, idents))
        lists.append(lexical.search(store, query[:80], limit))
    elif cat in ("documentation", "explanation"):
        lists.append(lexical.search(store, query[:80], limit))
        if keywords:
            lists.append(lexical.search(store, " OR ".join(keywords[:4]), limit))
    else:
        lists.append(lexical.search(store, query[:80], limit))
    sem_diag: dict = {}
    if mode == "hybrid_full":
        # Rank-space fusion (RRF): cosine scores never mix with lexical ranks.
        # Structural relations keep priority via the unchanged rerank stage.
        # hybrid_full intentionally has no abstention (documented); use the
        # `semantic` mode or deterministic strategies when abstention matters.
        lists.append(_semantic_candidates(store, cfg, provider, query, limit, sem_diag))
    if mode == "semantic":
        return _execute_semantic(store, query, limit, cfg, provider, p,
                                 evm.freshness(store))
    fused = fusion.rrf(lists)
    id2 = {}
    for lst in lists:
        for n in lst:
            nid = n.id if hasattr(n, "id") else n.get("id")
            id2[nid] = n
    ranked = [id2[k] for k, _ in fused if k in id2][:limit]
    ranked = reranking.rerank(ranked, query)
    arch_note = None
    if cat == "architecture_overview":
        # Structural questions want structural answers: modules/files/packages
        # first (stable sort preserves RRF order within each group).
        rank = {"module": 0, "package": 0, "directory": 0, "file": 1}
        ranked = sorted(ranked, key=lambda n: rank.get(getattr(n, "type", ""), 2))
        arch_note = None
        if not any(getattr(n, "type", "") in ("module", "package", "directory", "file")
                   for n in ranked):
            # No lexical foothold (e.g. "package structure" matches no node
            # text): fall back to the module inventory, stated as such.
            inv = [n for n in store.all_nodes(limit=5000)
                   if getattr(n, "type", "") in ("module", "package")]
            inv.sort(key=lambda n: getattr(n, "qualified_name", "") or "")
            if inv:
                ranked = inv[:limit]
                arch_note = ("no lexical match for architecture terms; "
                             "returning module inventory instead")
    return _finish(store, ranked, p, cat, fused, idents, keywords, scope, t0,
                   sem_diag, mode, extra_limitations=[arch_note] if arch_note else None)


def _execute_semantic(store, query: str, limit: int, cfg, provider, plan_d: dict, fresh: dict) -> dict:
    """Semantic-only retrieval with the calibrated abstention policy."""
    from ..retrieval import semantic as sem, abstention as abst
    from ..retrieval import lexical as lexmod, terms as termex
    t0 = time.time()
    meta = {**plan_d, "mode": "semantic", "latency_s": 0.0}
    tex = termex.extract_terms(query)
    if cfg is None or not getattr(cfg, "embedding_model", ""):
        return {"answer": "No evidence in graph establishes an answer.", "facts": [],
                "evidence": [], "graph_paths": [],
                "limitations": ["semantic retrieval is off (no embedding_model configured)"],
                "freshness": fresh,
                "retrieval_metadata": {**meta, "abstention": {"verdict": "abstain",
                                                              "reason": "provider-unavailable"}}}
    hits, scores, stale = _semantic_hits(store, cfg, provider, query, limit)
    if hits is None:
        return {"answer": "No evidence in graph establishes an answer.", "facts": [],
                "evidence": [], "graph_paths": [],
                "limitations": ["semantic provider or index unavailable"],
                "freshness": fresh,
                "retrieval_metadata": {**meta, "abstention": {"verdict": "abstain",
                                                              "reason": "provider-unavailable"}}}
    lex_ids = {n.id for n in lexmod.search(store, query[:80], 20)}
    idents = {i.lower() for i in tex["identifiers"]}
    verdict = abst.decide(abst.SemanticSignals(
        max_score=scores[0] if scores else 0.0, n_results=len(hits),
        lexical_overlap=any(h.id in lex_ids for h in hits),
        identifier_hit=any(((h.label or "").lower() in idents
                            or (h.qualified_name or "").lower() in idents) for h in hits),
        index_stale=bool(stale)), tau=abst.TAU)
    meta["abstention"] = verdict
    meta["latency_s"] = round(time.time() - t0, 3)
    if verdict["verdict"] == "abstain":
        return {"answer": "No sufficiently relevant semantic evidence found.",
                "facts": [], "evidence": [], "graph_paths": [],
                "limitations": [f"abstained: {verdict['reason']}"],
                "freshness": fresh, "retrieval_metadata": meta}
    facts = [{"id": n.id, "type": n.type, "label": n.label,
              "qualified_name": n.qualified_name, "file": n.file_path,
              "line": n.location.start_line if hasattr(n.location, "start_line") else 1,
              "score": scores[i] if i < len(scores) else 0.0}
             for i, n in enumerate(hits[:limit])]
    if verdict["verdict"] == "ambiguous":
        lims = [f"ambiguous: {verdict['reason']}"]
    else:
        lims = []
    return {"answer": f"Top semantic match: {hits[0].qualified_name}." if hits else "none",
            "facts": facts, "evidence": evm.evidence_for(store, [n.id for n in hits[:limit]]),
            "graph_paths": [], "limitations": lims, "freshness": fresh,
            "retrieval_metadata": meta}


def _semantic_hits(store, cfg, provider, query: str, limit: int):
    """(nodes, scores, stale) or None when provider/index unavailable."""
    from ..retrieval import semantic as sem
    model = getattr(cfg, "embedding_model", "")
    try:
        if provider is None:
            from ..retrieval.embeddings import OllamaEmbeddingProvider, EmbeddingConfig
            provider = OllamaEmbeddingProvider(EmbeddingConfig(
                endpoint=getattr(cfg, "embedding_endpoint", "http://localhost:11434"),
                model=model,
                allow_remote=bool(getattr(cfg, "allow_remote_embeddings", False))))
        res = sem.search(store, provider, model, query, limit=limit)
    except Exception:
        return None
    if not res.get("available"):
        return None
    out, scores = [], []
    for hit in res.get("results", [])[:limit]:
        n = store.get_node(hit["id"])
        if n is not None:
            out.append(n)
            scores.append(hit.get("score", 0.0))
    gap = (scores[0] - scores[1]) if len(scores) >= 2 else 1.0
    stale = (res.get("stale") or {}).get("stale", False)
    return out, scores, stale


def _finish(store, ranked, p, cat, fused, idents, keywords, scope, t0,
            sem_diag, mode, extra_limitations=None):
    feats = [{"id": n.id, "type": n.type, "label": n.label,
              "qualified_name": n.qualified_name, "file": n.file_path,
              "line": n.location.start_line if hasattr(n.location, "start_line") else 1}
             for n in ranked]
    ev = evm.evidence_for(store, [n.id for n in ranked])
    paths: list = []
    path_limitations: list[str] = []
    multihop_diag: dict = {}
    if cat == "dependency_tracing" and idents:
        from ..retrieval import multihop as _mh
        seeds = _mh.discover_seeds(store, idents)
        eps = seeds["endpoints"]
        if len(eps) >= 2 and (not eps[0]["candidates"] or not eps[-1]["candidates"]):
            missing = [e["identifier"] for e in (eps[0], eps[-1]) if not e["candidates"]]
            path_limitations.append("no candidate symbols for endpoint(s): "
                                    + ", ".join(missing))
        if len(eps) >= 2 and eps[0]["candidates"] and eps[-1]["candidates"]:
            # Ambiguity never blocks the search: candidate pairs are tried in
            # order and the first validated ranking wins; the ambiguity itself
            # stays recorded in diagnostics (not silently collapsed).
            srcs = [n for n in eps[0]["candidates"]]
            dsts = [n for n in eps[-1]["candidates"]]
            ranked_p: list = []
            found = {"paths": [], "expanded": 0, "budget_hit": None}
            for cs in srcs:
                for ct in dsts:
                    f = _mh.find_paths(store, [cs.id], [ct.id])
                    r = _mh.rank_paths(store, f["paths"])
                    found["expanded"] += f["expanded"]
                    found["budget_hit"] = found["budget_hit"] or f["budget_hit"]
                    if r:
                        ranked_p, found = r, f
                        break
                if ranked_p:
                    break
            for rp in ranked_p[:3]:
                paths.append(rp["nodes"])
            multihop_diag = {"seeds": {e["identifier"]: [n.id for n in e["candidates"]]
                                       for e in eps},
                             "ambiguous": [e["identifier"] for e in eps if e["ambiguous"]],
                             "expanded": found["expanded"], "budget_hit": found["budget_hit"],
                             "ranked": _mh.explain(store, ranked_p),
                             "weights": dict(_mh.PATH_WEIGHTS)}
            if not paths:
                path_limitations.append("no directed path between the named endpoints "
                                        "within the configured bounds")
        elif len(ranked) >= 2:
            # Fallback: no named endpoints; keep the old ranked-pair behavior.
            from ..graph.algorithms import validate_path, shortest_path as _sp
            edges = store.all_edges()
            eo = [type("E", (), {"source": e["source"], "target": e["target"],
                                 "relation": e["relation"], "id": e["id"]})()
                  for e in edges]
            path = _sp(ranked[0].id, ranked[1].id, eo)
            if path:
                check = validate_path(path, edges)
                if check["valid"]:
                    paths.append(path)
                else:
                    path_limitations.append(f"candidate path failed validation: {check['reason']}")
    fresh = evm.freshness(store)
    meta = {**p, "latency_s": round(time.time() - t0, 3), "candidates": len(fused),
            "identifiers": idents, "keywords": keywords, "scope": scope,
            "ranking": dict(reranking.RERANK_WEIGHTS), "mode": mode}
    if sem_diag:
        meta["semantic"] = sem_diag.get("semantic")
    if multihop_diag:
        meta["multihop"] = multihop_diag
    if not ranked:
        return {"answer": "No evidence in graph establishes an answer.", "facts": [],
                "evidence": [], "graph_paths": [], "limitations": [
                    "no matching symbols; index may be stale or term absent"],
                "freshness": fresh, "retrieval_metadata": meta}
    ans = (f"Top match: {ranked[0].qualified_name or ranked[0].label} "
           f"({ranked[0].type}) at {ranked[0].file_path}:"
           f"{getattr(ranked[0].location, 'start_line', 1)}.")
    return {"answer": ans, "facts": feats, "evidence": ev, "graph_paths": paths,
            "limitations": path_limitations + list(extra_limitations or []),
            "freshness": fresh, "retrieval_metadata": meta}
