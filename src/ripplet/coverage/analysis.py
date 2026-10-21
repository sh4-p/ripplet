"""Coverage analysis and risk-aware test intelligence (Phase 8 §6/7).

Every number names its denominator and source run. Absent data is `unknown`,
never `uncovered`. Priority scores are documented heuristics, never proof of a
defect. No structural edges are created here.
"""
from __future__ import annotations
import json


def _run_or_raise(store, run_id: str) -> dict:
    from . import store as cov_store
    run = cov_store.get_run(store, run_id)
    if run is None:
        raise ValueError(f"unknown coverage run: {run_id}")
    return run


def freshness_of(store, run_id: str) -> dict:
    try:
        r = store.cx.execute("SELECT * FROM coverage_run_state WHERE run_id=?",
                             (run_id,)).fetchone()
        if r:
            return {"freshness": r["freshness"],
                    "stale_files": json.loads(r["stale_files"] or "[]")}
    except Exception:
        pass
    return {"freshness": "unknown", "stale_files": []}


def summary(store, run_id: str) -> dict:
    """Project-level metrics with explicit denominators. Branch fields are
    `unavailable` (not zero) when the report lacks branch data."""
    run = _run_or_raise(store, run_id)
    files = [dict(r) for r in store.cx.execute(
        "SELECT * FROM coverage_files WHERE run_id=?", (run_id,)).fetchall()]

    def _pct(a, b):
        return round(100.0 * a / b, 2) if b else None

    lines_hit = sum(f["lines_hit"] or 0 for f in files)
    lines_missed = sum(f["lines_missed"] or 0 for f in files)
    denom_lines = lines_hit + lines_missed
    f_found = [f["funcs_found"] for f in files if f["funcs_found"] is not None]
    f_cov = [f["funcs_covered"] for f in files if f["funcs_covered"] is not None]
    b_found = [f["branches_found"] for f in files if f["branches_found"] is not None]
    b_taken = [f["branches_taken"] for f in files if f["branches_taken"] is not None]
    unmapped = sum(1 for f in files if f["match_status"] == "unmapped")
    amb = store.cx.execute(
        "SELECT COUNT(*) c FROM coverage_symbol_map WHERE run_id=? AND confidence='ambiguous'",
        (run_id,)).fetchone()["c"]
    out = {
        "run_id": run_id, "tool": run["tool"], "format": run["format"],
        "label": run["label"], "revision_status": run["revision_status"],
        "status": run["status"],
        "files_with_coverage": len(files), "unmapped_files": unmapped,
        "lines": {"hit": lines_hit, "missed": lines_missed,
                  "denominator": denom_lines,
                  "pct": _pct(lines_hit, denom_lines), "source": "observed line hits"},
        "functions": {"found": sum(f_found) if f_found else None,
                      "covered": sum(f_cov) if f_cov else None,
                      "denominator": sum(f_found) if f_found else None,
                      "pct": _pct(sum(f_cov), sum(f_found)) if f_found else None,
                      "source": "format-supplied function counters"},
        "branches": {"found": sum(b_found) if b_found else None,
                     "taken": sum(b_taken) if b_taken else None,
                     "denominator": sum(b_found) if b_found else None,
                     "pct": _pct(sum(b_taken), sum(b_found)) if b_found else None,
                     "source": "format-supplied branch counters",
                     "available": bool(b_found)},
        "ambiguous_mappings": amb,
        **freshness_of(store, run_id),
    }
    return out


def symbol_coverage(store, symbol_id: str, run_id: str = "") -> dict:
    """Evidence for one symbol: explicit zero-hits are `uncovered_in_run`;
    no observation is `unknown`/`no_coverage_data` — never `uncovered`."""
    node = store.get_node(symbol_id)
    if node is None:
        return {"symbol": symbol_id, "status": "unknown_symbol"}
    runs = [r["id"] for r in store.cx.execute(
        "SELECT id FROM coverage_runs" + (" WHERE id=?" if run_id else ""),
        (run_id,) if run_id else ()).fetchall()]
    per_run = []
    for rid in runs:
        rows = [dict(r) for r in store.cx.execute(
            "SELECT * FROM coverage_symbol_map WHERE run_id=? AND symbol_id=?",
            (rid, symbol_id)).fetchall()]
        if not rows:
            per_run.append({"run_id": rid, "status": "no_coverage_data"})
            continue
        lines = sorted({(r["file"], r["line"]) for r in rows})
        hits = []
        for rel, ln in lines:
            h = store.cx.execute(
                "SELECT hits FROM coverage_lines WHERE run_id=? AND file=? AND line=?",
                (rid, rel, ln)).fetchone()
            hits.append(h["hits"] if h else None)
        if any(h is None for h in hits):
            status = "unknown"
        elif all(h == 0 for h in hits):
            status = "uncovered_in_run"
        elif all((h or 0) > 0 for h in hits):
            status = "covered_in_run"
        else:
            status = "partially_covered_in_run"
        per_run.append({"run_id": rid, "status": status, "lines": len(lines),
                        "methods": sorted({r["method"] for r in rows})})
    operating = [p for p in per_run if p["status"] not in ("no_coverage_data",)]
    overall = "no_coverage_data" if not operating else operating[0]["status"]
    return {"symbol": node.qualified_name, "file": node.file_path,
            "overall": overall, "runs": per_run}


def uncovered_symbols(store, run_id: str, scope: str = "", limit: int = 50) -> list[dict]:
    """Symbols with explicit zero-hit observations in this run (and revision).

    Single set-based query (no per-symbol round trips): a symbol qualifies iff
    it has mapped lines in this run and every mapped hit is an explicit zero.
    """
    _run_or_raise(store, run_id)
    rows = store.cx.execute(
        "SELECT m.symbol_id, n.qualified_name, n.file_path,"
        " SUM(CASE WHEN l.hits = 0 THEN 1 ELSE 0 END) AS zeroes,"
        " SUM(CASE WHEN l.hits IS NULL THEN 1 ELSE 0 END) AS unknowns,"
        " COUNT(*) AS n"
        " FROM coverage_symbol_map m"
        " LEFT JOIN coverage_lines l ON l.run_id = m.run_id AND l.file = m.file"
        "  AND l.line = m.line"
        " LEFT JOIN nodes n ON n.id = m.symbol_id"
        " WHERE m.run_id=? GROUP BY m.symbol_id", (run_id,)).fetchall()
    out = []
    for r in rows:
        if (r["zeroes"] or 0) > 0 and (r["zeroes"] or 0) == (r["n"] or 0) \
                and (r["unknowns"] or 0) == 0:
            if scope and scope not in (r["file_path"] or ""):
                continue
            out.append({"symbol": r["qualified_name"], "file": r["file_path"],
                        "status": "uncovered_in_run", "run_id": run_id})
            if len(out) >= limit:
                break
    return sorted(out, key=lambda x: x["symbol"])[:limit]


def prioritize(store, run_id: str, limit: int = 20) -> list[dict]:
    """Risk-aware ordering of coverage gaps. Heuristic scores with stated
    factors; importance never implies a defect, absence never implies coverage."""
    from ..graph import impact as _impact
    _run_or_raise(store, run_id)
    edges = store.all_edges()
    nodes = store.all_nodes()
    cands = uncovered_symbols(store, run_id, limit=500)
    scored = []
    for c in cands:
        node = next((n for n in nodes if n.qualified_name == c["symbol"]), None)
        if node is None:
            continue
        dependents = _impact.analyze([node.id], _eadapt(edges), max_depth=2)
        n_dep = len({d["affected"] for d in dependents})
        tests = _impact.find_tests(nodes, _eadapt(edges), [node.id])
        indegree = sum(1 for e in edges if e["target"] == node.id)
        factors = {"explicitly_uncovered": 3.0, "dependents": min(n_dep, 10) * 0.5,
                   "indegree": min(indegree, 20) * 0.1,
                   "has_related_tests": 1.0 if tests else 0.0}
        scored.append({"symbol": c["symbol"], "file": c["file"],
                       "score": round(sum(factors.values()), 2), "factors": factors,
                       "dependents": n_dep, "related_tests": tests[:5],
                       "run_id": run_id})
    scored.sort(key=lambda x: (-x["score"], x["symbol"]))
    return scored[:limit]


def _eadapt(edges):
    out = []
    for e in edges:
        o = type("E", (), {})()
        o.source, o.target, o.relation, o.id = \
            e["source"], e["target"], e["relation"], e["id"]
        out.append(o)
    return out


def compare(store, base_id: str, head_id: str) -> dict:
    """Compare two runs. Methodology: per-file/per-line set differences on raw
    observations; source renames or instrumentation changes are flagged as
    mapping differences, never as silent regressions."""
    from . import store as cov_store
    base = cov_store.get_run(store, base_id)
    head = cov_store.get_run(store, head_id)
    if base is None or head is None:
        raise ValueError("unknown run id in comparison")
    # Same report bytes in, same observations out: equal hashes mean the two
    # runs were computed from identical source inputs (Phase 31: was `!=`).
    same_source_inputs = (base["report_hash"] == head["report_hash"])

    def _lines(rid):
        return {(r["file"], r["line"]): r["hits"] for r in store.cx.execute(
            "SELECT file, line, hits FROM coverage_lines WHERE run_id=?", (rid,)).fetchall()}

    bl, hl = _lines(base_id), _lines(head_id)
    newly_covered = sorted(k for k in hl if hl[k] > 0 and bl.get(k, 0) == 0)
    newly_uncovered = sorted(k for k in bl if bl[k] > 0 and hl.get(k, 0) == 0)
    gone = sorted(set(bl) - set(hl))
    added = sorted(set(hl) - set(bl))
    return {"method": "raw per-line set difference; renames/deletions reported "
                      "separately, never scored as regressions",
            "base": base_id, "head": head_id,
            "same_source_inputs": same_source_inputs,
            "newly_covered": newly_covered, "newly_uncovered": newly_uncovered,
            "removed_lines": gone, "added_lines": added,
            "counts": {"base_lines": len(bl), "head_lines": len(hl)}}


def mapping_diagnostics(store, run_id: str, limit: int = 50) -> dict:
    _run_or_raise(store, run_id)
    amb = [dict(r) for r in store.cx.execute(
        "SELECT * FROM coverage_symbol_map WHERE run_id=? AND confidence='ambiguous'"
        " LIMIT ?", (run_id, limit)).fetchall()]
    files = [dict(r) for r in store.cx.execute(
        "SELECT * FROM coverage_files WHERE run_id=? AND match_status='unmapped'"
        " LIMIT ?", (run_id, limit)).fetchall()]
    return {"run_id": run_id, "ambiguous": amb, "unmapped_files": files,
            **freshness_of(store, run_id)}
