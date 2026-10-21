from __future__ import annotations
ORDER = ["deterministic","resolved","observed","claimed","hypothetical","conflicted","stale","unavailable"]
def rank(cc: str) -> int:
    try: return ORDER.index(cc)
    except ValueError: return 99
def is_deterministic(cc: str) -> bool: return cc in ("deterministic","resolved")
def describe(edge) -> str:
    return f"{edge.evidence_type}/{edge.confidence_class}" + (f" p={edge.confidence_score}" if edge.confidence_score is not None else "")
