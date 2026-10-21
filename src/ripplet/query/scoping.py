"""Task-aware scoped query_graph responses (Phase 22).

Opt-in projection over the legacy execute() response: same facts and
evidence rows, fewer of them per declared scope, with explicit totals,
truncation markers, and continuation affordances. Provenance is never
weakened (stable ids, file/line, ambiguity and unresolved markers ride
along); omitted content is named, never implied absent; full evidence
stays retrievable via get_node/get_neighbors/expand_evidence.

Reuses Phase 18/19 machinery instead of a new serialization: evidence
trimming happens before spanify_response (spans stay recoverable), and
the static ranking-weights dict is replaced by factpack.weights_fp.
Absent scope = legacy response untouched.
"""
from __future__ import annotations
import copy

SCOPES = ("disambiguate", "deps", "path", "impact", "tests", "explore")

# Per-scope budgets: facts shown, evidence rows shown, keep paths.
_SCOPE_BUDGETS = {
    "disambiguate": {"facts": None, "evidence": 3, "paths": False},
    "deps": {"facts": None, "evidence": 3, "paths": False},
    "path": {"facts": 10, "evidence": 3, "paths": True},
    "impact": {"facts": None, "evidence": 5, "paths": True},
    "tests": {"facts": None, "evidence": 3, "paths": False},
    "explore": {"facts": None, "evidence": 10, "paths": True},
}

_SCOPE_HINTS = {
    "disambiguate": "several same-name candidates above; use get_neighbors --id <id> to tell them apart by callers",
    "deps": "direct dependencies above; use get_neighbors --id <id> --direction both for the full neighborhood",
    "path": "bounded path(s) above; use find_path --source X --target Y for other endpoint pairs",
    "impact": "directly affected nodes above; use analyze_change_impact for deeper traversal",
    "tests": "related test files above; use find_related_tests --symbols X --compact for the file list",
    "explore": "top facts above; narrow with a scope for cheaper follow-ups",
}


def strip_fact_texts(res: dict) -> dict:
    """Drop span_text from scoped facts (ranges + has_more stay; full text
    recovers via expand_evidence --node_id <id> --level span). Records the
    omission in scope_info so nothing is silently lost."""
    if not isinstance(res, dict):
        return res
    n = 0
    for f in res.get("facts") or []:
        if isinstance(f, dict) and "span_text" in f:
            del f["span_text"]
            n += 1
    info = res.get("scope_info")
    if isinstance(info, dict) and n:
        omitted = info.get("omitted") or []
        omitted.append(
            "fact span_text[%d withheld; expand_evidence --node_id <id> "
            "--level span recovers it]" % n)
        info["omitted"] = omitted
    return res


def apply_scope(res: dict, scope: str, limit: int = 20) -> dict:
    """Project a legacy query_graph response into a scoped response."""
    if scope not in _SCOPE_BUDGETS:
        raise ValueError(f"unknown scope: {scope!r} (expected one of {SCOPES})")
    if not isinstance(res, dict):
        raise ValueError("scoped mode projects a response dict")
    out = copy.deepcopy(res)
    budget = _SCOPE_BUDGETS[scope]
    omitted: list[str] = []

    facts = out.get("facts") if isinstance(out.get("facts"), list) else []
    fact_cap = budget["facts"] if budget["facts"] is not None else limit
    shown_facts = facts[:fact_cap]
    out["facts"] = shown_facts

    ev = out.get("evidence") if isinstance(out.get("evidence"), list) else []
    ev_cap = budget["evidence"]
    out["evidence"] = ev[:ev_cap]

    paths = out.get("graph_paths") if isinstance(out.get("graph_paths"), list) else []
    if budget["paths"]:
        out["graph_paths"] = paths[:2]
    else:
        if paths:
            omitted.append(f"graph_paths[{len(paths)} withheld by scope]")
        out["graph_paths"] = []

    if len(facts) > len(shown_facts):
        omitted.append(f"facts[{len(shown_facts)}:{len(facts)} withheld by scope]")
    if len(ev) > len(out["evidence"]):
        omitted.append(f"evidence[{len(out['evidence'])}:{len(ev)} withheld by scope]")

    meta = out.get("retrieval_metadata")
    if isinstance(meta, dict) and isinstance(meta.get("ranking"), dict):
        from .factpack import weights_fp
        meta = dict(meta)
        meta["ranking"] = {"weights_fp": weights_fp(meta["ranking"])}
        out["retrieval_metadata"] = meta
        omitted.append("retrieval_metadata.ranking(full weights -> fingerprint)")

    total_facts = len(facts)
    total_ev = len(ev)
    out["scope_info"] = {
        "scope": scope,
        "facts_total": total_facts,
        "facts_shown": len(shown_facts),
        "evidence_total": total_ev,
        "evidence_shown": len(out["evidence"]),
        "paths_shown": len(out["graph_paths"]),
        "omitted": omitted,
        "complete": len(shown_facts) >= total_facts and
                    len(out["evidence"]) >= total_ev,
        "continuation": None if (len(shown_facts) >= total_facts) else {
            "hint": "repeat with a larger limit for more facts; "
                    "per-fact depth via get_node <id>"},
        "expansion": _SCOPE_HINTS[scope],
    }
    return out
