"""Plan-vs-run comparison (Phase 10 Stage 8). Read-only, deterministic.

Answers: which planned tests ran, which were skipped, pass/fail, selection
coverage, missing-from-collection, source drift since approval, coverage
evidence produced, uninterpretable parts. Outcomes never re-judge selection
quality: failures don't prove good selection, passes don't prove effective
selection (stated in the report itself).
"""
from __future__ import annotations


def _base(node: str) -> str:
    return (node or "").split("[")[0]


def compare_plan_run(plan: dict, run: dict, current_fingerprint: dict | None = None) -> dict:
    planned = sorted({(s.get("test_id") or "") for s in run.get("selection", [])
                      if s.get("test_id")})
    planned_sel = sorted({(s.get("selector") or "") for s in run.get("selection", [])
                          if s.get("selector")})
    per_test = run.get("per_test", []) or []
    by_base: dict[str, list[dict]] = {}
    for p in per_test:
        by_base.setdefault(_base(p.get("node", "")), []).append(p)
    def _covers(sel: str) -> bool:
        # An exact node selector is covered by its own report; a planned
        # file-scope selector is covered by any executed node under it.
        b = _base(sel)
        if "::" in b:
            return b in by_base
        return any(node_b == b or node_b.startswith(b + "::")
                   for node_b in by_base)

    executed_ids: set[str] = {sel for sel in planned_sel if _covers(sel)}
    # Also match planned test_ids whose selector form coincides.
    planned_tests = sorted({(s.get("display") or s.get("selector") or "")
                            for s in run.get("selection", [])})
    passed, failed, skipped, errors, unknown = [], [], [], [], []
    for p in per_test:
        o = (p.get("outcome") or "unknown").lower()
        if o == "passed":
            passed.append(p["node"])
        elif o == "failed":
            failed.append(p["node"])
        elif o == "skipped":
            skipped.append(p["node"])
        elif o == "error":
            errors.append(p["node"])
        else:
            unknown.append(p["node"])
    skipped_planned = sorted(set(planned_sel) - executed_ids)
    # Missing from collection is stricter: exact node selectors the runner
    # never reported (deselected, errored, or renamed). File scopes are
    # covered by any node beneath them, so they are never "missing" when
    # their tests ran.
    missing_from_collection = sorted(
        s for s in planned_sel
        if "::" in _base(s) and _base(s) not in by_base)
    approval_fp = run.get("source_fingerprint", {}) or {}
    drift = []
    if current_fingerprint is not None:
        drift = sorted(rel for rel, sha in approval_fp.items()
                       if current_fingerprint.get(rel) != sha)
    cov_id = run.get("coverage_run_id", "") or ""
    # Provenance identity: a comparison is only meaningful for the run's own
    # plan at the planned revision. Mismatches are reported, never silent —
    # an unrelated plan/run pair otherwise reads as a real comparison.
    plan_id = plan.get("plan_id", "") if isinstance(plan, dict) else ""
    match = None
    plan_warnings: list[str] = []
    if plan_id:
        match = (run.get("plan_id", "") == plan_id)
        if not match:
            plan_warnings.append(
                f"run {run.get('id', '')} belongs to plan "
                f"{run.get('plan_id', '')}, not {plan_id}: "
                "treat every overlap below as coincidental")
    plan_rev = plan.get("revision", "") if isinstance(plan, dict) else ""
    run_rev = run.get("source_revision", "") or ""
    revision_match = None
    if plan_rev and run_rev:
        revision_match = (plan_rev == run_rev)
        if not revision_match:
            plan_warnings.append(
                f"plan revision {plan_rev} != run revision {run_rev}: "
                "outcomes may reflect drift, not selection quality")
    uninterpretable = []
    if plan_warnings:
        uninterpretable.extend(plan_warnings)
    if (run.get("parse_status") or "") != "ok":
        uninterpretable.append(
            f"parse_status={run.get('parse_status')}: individual outcomes "
            f"may be unavailable (unreported={run.get('unreported', 0)})")
    if run.get("output_truncated"):
        uninterpretable.append("captured output was truncated at the byte cap")
    if run.get("outcome") in ("timed_out", "cancelled"):
        uninterpretable.append(f"run did not complete normally: {run.get('outcome')}")
    notes = [
        "a test failure does not prove Ripplet selected the correct test;",
        "a test passing does not prove the selection was effective;",
        "selection quality is measured on independently annotated benchmarks, "
        "not on execution outcomes.",
    ]
    return {
        "plan_id": plan.get("plan_id", "") if isinstance(plan, dict) else "",
        "run_id": run.get("id", ""),
        "plan_run_match": match,
        "revision_match": revision_match,
        "planned": planned,
        "planned_selectors": planned_sel,
        "planned_tests": planned_tests,
        "executed_selectors": sorted(executed_ids),
        "executed_count": len(by_base),
        "passed": sorted(passed), "failed": sorted(failed),
        "skipped": sorted(skipped), "errors": sorted(errors),
        "unknown": sorted(unknown),
        "skipped_planned": skipped_planned,
        "missing_from_collection": missing_from_collection,
        "selection_covered": not skipped_planned and not missing_from_collection,
        "source_drift_since_approval": drift,
        "coverage_run_id": cov_id,
        "coverage_attribution": run.get("coverage_attribution", ""),
        "uninterpretable": uninterpretable,
        "outcome": run.get("outcome", ""),
        "parse_status": run.get("parse_status", ""),
        "notes": notes,
    }


def run_signals(store, limit: int = 200,
                include_coverage_requesters: bool = False) -> dict:
    """Descriptive history signals (Stage 11). Aggregations only — no ranking
    changes, no automation, no relevance labels from passing runs.

    Opt-in `include_coverage_requesters` (Phase 31) additionally names the
    runs behind `coverage_requested_but_missing` (bounded, oldest first) so
    the advisory is actionable. Absent, the legacy shape is byte-identical.
    """
    from . import store as _st
    runs = _st.list_runs(store, limit=limit)
    full = []
    for r in runs:
        try:
            full.append(_st.get_run(store, r["id"]))
        except Exception:
            continue
    by_test_fail: dict[str, int] = {}
    by_test_run: dict[str, int] = {}
    gaps = 0
    stale_cov = 0
    missing_cov_ids: list[str] = []
    outcomes: dict[str, int] = {}
    for r in full or []:
        if not r:
            continue
        outcomes[r.get("outcome", "?")] = outcomes.get(r.get("outcome", "?"), 0) + 1
        for p in r.get("per_test", []) or []:
            node = p.get("node", "")
            by_test_run[node] = by_test_run.get(node, 0) + 1
            if (p.get("outcome") or "") in ("failed", "error"):
                by_test_fail[node] = by_test_fail.get(node, 0) + 1
        cmp = compare_plan_run({}, r, None)
        if not cmp["selection_covered"] and r.get("parse_status") == "ok":
            gaps += 1
        if r.get("coverage_enabled") and not r.get("coverage_run_id"):
            stale_cov += 1
            if include_coverage_requesters and len(missing_cov_ids) < 50:
                missing_cov_ids.append(r.get("id", ""))
    repeat_failures = sorted(
        ((n, c) for n, c in by_test_fail.items() if c >= 2),
        key=lambda x: (-x[1], x[0]))[:20]
    out = {
        "runs_considered": len(full),
        "outcomes": outcomes,
        "repeated_failures": [{"node": n, "failures": c} for n, c in repeat_failures],
        "plan_executed_gaps": gaps,
        "coverage_requested_but_missing": stale_cov,
        "note": ("descriptive signals only: passing runs are not relevance "
                 "labels, and no ranking weight is adjusted by this report."),
    }
    if include_coverage_requesters:
        out["coverage_missing_run_ids"] = sorted(missing_cov_ids)
    return out
