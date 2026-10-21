"""Calibrated abstention for semantic-only retrieval (Phase 5 Stage 5).

Cosine similarity is NOT a probability of correctness. The policy below only
decides whether semantic evidence may be shown at all; it never overrides exact
identifier results and never manufactures structural relationships.

Verdicts: supported | ambiguous | abstain:<reason>. Reasons: provider-unavailable,
index-missing, index-stale, validation-failure, insufficient-similarity,
no-independent-support.

The default threshold TAU is set by live calibration on the DEV split only
(see docs + gate artifacts); held-out assessment decides whether it ships.
TAU = 0.0 reproduces legacy behavior (score never abstains).
"""
from __future__ import annotations
from dataclasses import dataclass

# Calibration attempt (dev split, qwen2.5-coder:latest 3584d, 2026-10-12):
# answerable max-cosine 0.072..0.649 (n=70) vs unanswerable 0.174..0.386 (n=6)
# overlap almost everywhere — distributions overlap, no separating threshold
# exists for this model (best grid accuracy 0.22 at tau=0.0). Per policy below,
# NO threshold is therefore adopted: TAU stays 0.0 (legacy behavior) and this
# constant must NOT be read as calibrated. Re-calibrate per model.
TAU = 0.0
TAU_PROVENANCE = ("uncalibrated default; live calibration attempted 2026-10-12 "
                  "with qwen2.5-coder:latest found no defensible threshold "
                  "(overlapping score distributions); see gate artifacts")
AMBIGUITY_MARGIN = 0.02


@dataclass
class SemanticSignals:
    max_score: float = 0.0
    n_results: int = 0
    lexical_overlap: bool = False
    identifier_hit: bool = False
    index_stale: bool = False
    index_missing: bool = False
    provider_ok: bool = True
    top_two_gap: float = 1.0


def decide(signals: SemanticSignals, tau: float = TAU,
           require_agreement: bool = True) -> dict:
    """Apply the abstention policy. Pure function, fully unit-testable."""
    if not signals.provider_ok:
        return {"verdict": "abstain", "reason": "provider-unavailable"}
    if signals.index_missing:
        return {"verdict": "abstain", "reason": "index-missing"}
    if signals.index_stale:
        return {"verdict": "abstain", "reason": "index-stale"}
    if signals.n_results == 0:
        return {"verdict": "abstain", "reason": "validation-failure"}
    if signals.max_score < tau:
        return {"verdict": "abstain", "reason": "insufficient-similarity",
                "detail": f"max_cosine={signals.max_score:.3f} < tau={tau:.3f}"}
    if require_agreement and not (signals.lexical_overlap or signals.identifier_hit):
        return {"verdict": "abstain", "reason": "no-independent-support"}
    if signals.top_two_gap < AMBIGUITY_MARGIN and signals.n_results >= 2:
        return {"verdict": "ambiguous",
                "reason": "near-tied semantic candidates; corroborate before trusting"}
    return {"verdict": "supported", "reason": "above-threshold with independent support"}


def calibrate_threshold(rows: list[dict]) -> dict:
    """Grid-search tau on DEV rows only.

    Row: {answerable: bool, max_score: float, overlap: bool, ident: bool}.
    Objective: abstention accuracy = correct-abstain on unanswerable +
    correct-return on answerable, with require_agreement=True. Ties break
    toward the higher tau (fewer false positives). Pure function.
    """
    cands = sorted({r["max_score"] for r in rows} | {0.0})
    grid = [0.0] + [round((a + b) / 2, 4) for a, b in zip(cands, cands[1:])] + [1.01]
    best = {"tau": 0.0, "accuracy": -1.0, "detail": {}}
    for tau in grid:
        ok = tot = 0
        fp = fa = 0
        for r in rows:
            tot += 1
            d = decide(SemanticSignals(max_score=r["max_score"], n_results=1,
                                       lexical_overlap=r["overlap"],
                                       identifier_hit=r["ident"],
                                       provider_ok=True), tau=tau,
                       require_agreement=True)
            returned = d["verdict"] in ("supported", "ambiguous")
            if r["answerable"] and returned:
                ok += 1
            elif not r["answerable"] and not returned:
                ok += 1
            elif not r["answerable"] and returned:
                fp += 1
            elif r["answerable"] and not returned:
                fa += 1
        acc = ok / tot if tot else 0.0
        if acc > best["accuracy"] or (acc == best["accuracy"] and tau > best["tau"]):
            best = {"tau": tau, "accuracy": round(acc, 4),
                    "detail": {"false_positives": fp, "false_abstentions": fa, "n": tot}}
    return best
