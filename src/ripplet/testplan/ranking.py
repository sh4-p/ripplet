"""Explainable ranking: explicit precedence rules, no opaque weights.

Order key (lower is better):
  1. evidence category: A < B < C (verified > static > inferred);
  2. revision compatibility: revision_ok True (or None for non-coverage)
     before False — stale coverage never outranks current evidence;
  3. directness: depth ascending (direct depth-1 before transitive);
  4. ambiguity: fewer ambiguous/unresolved steps first;
  5. path length, then stable test-id tiebreak.

Modes only change ADMISSION, never the order:
  conservative: A + depth-1 B with resolved edges;
  balanced: + transitive B/C with an explicit path (depth<=3);
  broad: everything discovered (depth<=4).

Relevance and confidence stay separate fields; the score column is a
documented ordinal (rank position), never a probability. `rationale`
names every feature that contributed (category, revision_ok, depth,
ambiguous_steps, path length) so the ranking is inspectable.
"""
from __future__ import annotations

MODES = ("conservative", "balanced", "broad")
MODE_DEPTH = {"conservative": 1, "balanced": 3, "broad": 4}


def admissible(cand: dict, mode: str) -> tuple[bool, str]:
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    cat, depth = cand.get("category", "C"), cand.get("depth", 99)
    if cat == "A":
        return True, "verified observation"
    if cat == "B":
        if mode == "conservative":
            ev = (cand.get("evidence") or {}).get("kind", "")
            if depth == 1 and ev in ("calls", "imports", "uses", "uses_fixture",
                                     "imports_from", "depends_on"):
                return True, "direct static relationship"
            return False, "conservative admits only depth-1 static edges"
        if depth <= MODE_DEPTH[mode] and (cand.get("path") or depth == 1):
            return True, "static relationship within depth"
        return False, "beyond mode depth"
    # C inferred
    if mode == "conservative":
        return False, "conservative excludes inferred candidates"
    if not cand.get("path") and (cand.get("evidence") or {}).get("kind") != \
            "naming_heuristic":
        return False, "inferred candidate without a path"
    if depth <= MODE_DEPTH[mode]:
        return True, "inferred with supporting path" if cand.get("path") else \
            "naming heuristic (weakest)"
    return False, "beyond mode depth"


def _ambiguity(cand: dict) -> int:
    explicit = int(cand.get("ambiguous_steps", 0) or 0)
    text = str(cand.get("evidence", "")) + str(cand.get("path", ""))
    return explicit + text.count("ambiguous") + text.count("unresolved")


def _fresh_rank(cand: dict) -> int:
    # Non-coverage candidates carry revision_ok=None and keep their
    # category/depth order; only stale coverage (False) sorts after.
    return 0 if cand.get("revision_ok") is not False else 1


def order_key(cand: dict) -> tuple:
    cat_rank = {"A": 0, "B": 1, "C": 2}.get(cand.get("category", "C"), 2)
    return (cat_rank, _fresh_rank(cand), cand.get("depth", 99),
            _ambiguity(cand), len(cand.get("path", [])),
            cand.get("test_id", ""))


def rank(candidates: list[dict], mode: str) -> tuple[list[dict], list[dict]]:
    """Returns (admitted, excluded). Admitted carry rank + rationale."""
    admitted, excluded = [], []
    for c in candidates:
        ok, why = admissible(c, mode)
        (admitted if ok else excluded).append({**c, "admission": why})
    admitted.sort(key=order_key)
    for i, c in enumerate(admitted, 1):
        c["rank"] = i
        c["rationale"] = (f"#{i}: category {c['category']} ({c['admission']}), "
                          f"depth {c.get('depth')}, ambiguous_steps "
                          f"{_ambiguity(c)}, path_len {len(c.get('path', []))}, "
                          f"revision_ok {c.get('revision_ok')}, "
                          f"via {c.get('via', '?')}")
        c["score_semantics"] = ("ordinal rank position only; higher rank "
                                "means stronger evidence precedence, not a "
                                "probability of failure")
    return admitted, excluded
