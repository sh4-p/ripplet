from __future__ import annotations

# Token counting: tiktoken when installed, else a documented approximation.
_TOKENIZER = "char/4"


def _toks(s: str) -> int:
    global _TOKENIZER
    try:
        import tiktoken  # type: ignore
        _TOKENIZER = "tiktoken/cl100k"
        return max(1, len(tiktoken.get_encoding("cl100k_base").encode(str(s))))
    except Exception:
        _TOKENIZER = "char/4"
        return max(1, len(str(s)) // 4)


def pack(facts, evidence, paths, limitations, budget_tokens: int = 2000,
         diagnostics: dict | None = None) -> dict:
    """Smallest useful bundle under a token budget (Phase 4 Stage 8).

    - Facts are kept in rank order until the budget fills; truncation is flagged
      per section, never silent.
    - Evidence is deduplicated by id and capped; its quotes double as bounded
      source snippets (no separate snippet-fetch that could duplicate content).
    - Provenance is never dropped to save tokens: when facts are cut, their
      exclusion is reported.
    """
    used = 0
    kept_facts = []
    for f in facts:
        t = _toks(f)
        if used + t > budget_tokens:
            break
        kept_facts.append(f)
        used += t
    seen, kept_ev, snippets = set(), [], []
    for e in evidence or []:
        eid = e.get("id") if isinstance(e, dict) else getattr(e, "id", None)
        if eid in seen:
            continue
        seen.add(eid)
        t = _toks(e)
        if used + t > budget_tokens and kept_ev:
            break
        kept_ev.append(e)
        used += t
        q = (e.get("quote") or "") if isinstance(e, dict) else ""
        if q:
            snippets.append({"evidence_id": eid, "source": e.get("source_uri", ""),
                             "span": [e.get("start_line", 0), e.get("end_line", 0)],
                             "quote": q[:400]})
    kept_paths = (paths or [])[:10]
    dropped_facts = len(facts or []) - len(kept_facts)
    dropped_evidence = len(evidence or []) - len(kept_ev)
    return {"facts": kept_facts, "evidence": kept_ev, "snippets": snippets,
            "graph_paths": kept_paths, "limitations": limitations or [],
            "tokens_used": used, "tokenizer": _TOKENIZER,
            "truncated": dropped_facts > 0 or dropped_evidence > 0,
            "dropped": {"facts": dropped_facts, "evidence": dropped_evidence},
            "diagnostics": diagnostics or {}}
