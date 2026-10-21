from __future__ import annotations
from ..core.models import ExtractionResult


def _occ_list(edge) -> list:
    try:
        occ = (edge.attributes or {}).get("occurrences") or []
        return [o for o in occ if isinstance(o, dict) and "line" in o]
    except Exception:
        return []


# Relations where every repeated edge is a separate call site worth keeping.
_CALL_SITE_RELS = {"calls", "calls_unresolved", "instantiates", "dispatches"}


def _ensure_occurrence(edge) -> None:
    """An extractor that does not write `occurrences` still records the
    edge's own line; make it the edge's first occurrence so a later
    duplicate (a second call from the same caller) adds its line instead of
    silently replacing it."""
    if getattr(edge, "relation", None) not in _CALL_SITE_RELS or _occ_list(edge):
        return
    line = getattr(edge, "start_line", None)
    if not isinstance(line, int) or line <= 0:
        return
    try:
        edge.attributes = dict(edge.attributes or {})
    except Exception:
        edge.attributes = {}
    edge.attributes["occurrences"] = [{"line": line, "end_line": getattr(edge, "end_line", None) or line,
                                       "col": None}]


def _union_occurrences(old, new) -> None:
    """Merge per-occurrence evidence on duplicate edge IDs (Phase 5 Stage 1).

    One logical edge keeps every observed call site: occurrences union by
    (line, end_line, col), sorted; the primary span tracks the first one so
    start_line stays deterministic without using positions as identity.
    """
    occ = {(o.get("line"), o.get("end_line"), o.get("col")): o
           for o in _occ_list(old)}
    for o in _occ_list(new):
        occ.setdefault((o.get("line"), o.get("end_line"), o.get("col")), o)
    if occ:
        merged = [occ[k] for k in sorted(occ, key=lambda k: tuple(-1 if not isinstance(v, int) else v for v in k))]
        try:
            old.attributes = dict(old.attributes or {})
        except Exception:
            old.attributes = {}
        old.attributes["occurrences"] = merged
        try:
            old.start_line = merged[0].get("line", old.start_line)
            old.end_line = merged[0].get("end_line", old.end_line)
        except Exception:
            pass


def merge_results(results: list[ExtractionResult]) -> ExtractionResult:
    nodes: dict[str, object] = {}; edges: dict[str, object] = {}; ev: dict[str, object] = {}; warns=[]
    for r in results:
        warns.extend(r.warnings)
        for n in r.nodes:
            if n.id not in nodes: nodes[n.id]=n
            else:
                old = nodes[n.id]
                old.provenance_ids = sorted(set(old.provenance_ids) | set(n.provenance_ids))
                old.last_seen_at = max(old.last_seen_at, n.last_seen_at)
        for e in r.edges:
            if e.id not in edges: edges[e.id]=e
            else:
                old = edges[e.id]
                old.provenance_ids = sorted(set(old.provenance_ids) | set(e.provenance_ids))
                _ensure_occurrence(old)
                _ensure_occurrence(e)
                _union_occurrences(old, e)
        for x in r.evidence:
            ev.setdefault(x.id, x)
    return ExtractionResult(list(nodes.values()), list(edges.values()), list(ev.values()), warns)


def carry_occurrences(attrs: dict, src_row) -> dict:
    """Resolvers turn a `calls_unresolved` row into a resolved edge; keep the
    row's call-site lines on it (they were dropped, so a caller with several
    calls looked like it had one)."""
    try:
        raw = src_row.get("attributes") if isinstance(src_row, dict) else None
        if isinstance(raw, str):
            import json as _json
            raw = _json.loads(raw or "{}")
        occ = (raw or {}).get("occurrences")
        if occ and "occurrences" not in (attrs or {}):
            attrs = dict(attrs or {})
            attrs["occurrences"] = occ
    except Exception:
        pass
    return attrs


def union_duplicate_occurrences(edges: list) -> None:
    """Edges sharing one id (several call rows bound to the same caller,
    callee and shape) are written last-wins; give each copy the union of
    their call-site lines first."""
    by_id: dict = {}
    for e in edges:
        by_id.setdefault(e.id, []).append(e)
    for same in by_id.values():
        if len(same) < 2:
            continue
        for e in same:
            _ensure_occurrence(e)
        first = same[0]
        for e in same[1:]:
            _union_occurrences(first, e)
        occ = (first.attributes or {}).get("occurrences")
        if occ:
            for e in same[1:]:
                e.attributes = dict(e.attributes or {}, occurrences=occ)
                e.start_line = first.start_line
