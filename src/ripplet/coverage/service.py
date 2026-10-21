"""Coverage import orchestration (Phase 8).

One transaction per import: report rows, file mapping, revision state, derived
symbol map, run evidence. Failures roll back to the previously valid coverage
state. Never executes repository code; never runs builds or package managers.
"""
from __future__ import annotations
import time
from pathlib import Path

from .adapters import base as adapters_base
from . import store as cov_store
from . import mapping as cov_map


def _repo_id_for(root: str) -> str:
    import hashlib
    return "repo-" + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:10]


def import_report(store, report_path: str, repo_root: str, label: str = "",
                  fmt: str = "", revision: str = "", confirm_current: bool = False,
                  project_id: str = "default",
                  max_bytes: int = 8 * 1024 * 1024) -> dict:
    """Import one coverage report file. Returns a structured summary."""
    t0 = time.time()
    raw = Path(report_path).read_bytes()
    if len(raw) > max_bytes:
        from .adapters.base import ReportRejected
        raise ReportRejected(f"report exceeds {max_bytes} bytes")
    repo_root_s = str(Path(repo_root).resolve())
    repo_id = _repo_id_for(repo_root_s)
    # Priority 0: coverage rows land in this database; a foreign database
    # must be rejected before any write, not discovered afterwards.
    from ..workspace_scope import check_scope as _check_scope
    from ..workspace_scope import record_scope as _record_scope
    _scope = _check_scope(roots=[repo_root_s],
                          storage_path=getattr(store, "path", ""),
                          intent="coverage-import")
    parsed = adapters_base.parse_bytes(raw, repo_id, label, fmt=fmt)
    # normalize report paths against the authorized root
    from .models import LineObs, BranchObs, FuncObs, CtxObs, FileObs
    unmapped = 0
    # Normalize once per distinct file (pathlib.resolve dominates profiles);
    # record rows then map through the dictionary.
    distinct = set()
    for coll in (parsed.lines, parsed.branches, parsed.funcs, parsed.contexts,
                 parsed.files):
        for item in coll:
            distinct.add(item.file)
    rel_of: dict[str, str | None] = {}
    for raw_path in sorted(distinct):
        rel_of[raw_path] = cov_map.normalize_report_path(raw_path, repo_root_s)
    unmapped = sum(1 for _fp, rel in rel_of.items() if rel is None)

    def _rel(raw_path: str):
        return rel_of.get(raw_path)

    lines, branches, funcs, contexts, files = [], [], [], [], []
    for ln in parsed.lines:
        if (r := _rel(ln.file)) is not None:
            lines.append(LineObs(r, ln.line, ln.hits))
    for b in parsed.branches:
        if (r := _rel(b.file)) is not None:
            branches.append(BranchObs(r, b.line, b.block, b.branch, b.taken))
    for fn in parsed.funcs:
        if (r := _rel(fn.file)) is not None:
            funcs.append(FuncObs(r, fn.name, fn.line, fn.hits))
    for cx in parsed.contexts:
        if (r := _rel(cx.file)) is not None:
            contexts.append(CtxObs(cx.context, r, cx.line, cx.hits))
    for f in parsed.files:
        if (r := _rel(f.file)) is not None:
            files.append(FileObs(r, f.statements_found, f.statements_covered,
                                 f.lines_hit, f.lines_missed, f.funcs_found,
                                 f.funcs_covered, f.branches_found, f.branches_taken,
                                 f.match_status))
    parsed.lines, parsed.branches = lines, branches
    parsed.funcs, parsed.files, parsed.contexts = funcs, files, contexts
    if unmapped:
        parsed.warnings.append(f"{unmapped} records outside the repository root were dropped")
        parsed.partial = True
    run = parsed.run
    run.source_fps = {f.file: cov_map.file_fingerprint(repo_root_s, f.file)
                      for f in parsed.files}
    status, notes = cov_map.verify_revision(
        {"git_rev": run.git_rev}, repo_root_s, user_rev=revision,
        confirm_current=confirm_current)
    run.revision_status = status
    if confirm_current:
        run.meta = dict(run.meta or {})
        run.meta["revision_override"] = {"by": "user", "at": time.time(), "notes": notes}
    if parsed.partial and run.status == "complete":
        run.status = "partial"
    if not parsed.lines and not parsed.funcs and not parsed.branches:
        from .adapters.base import ReportRejected
        raise ReportRejected("report yielded no usable observations")
    try:
        cov_store.write_run(store, parsed, repo_id, project_id)
        from ..core.provenance import make_evidence
        from ..core.identifiers import stable_id
        ev = make_evidence(f"coverage:{run.id}",
                           f"{run.tool} {run.format} {run.label} "
                           f"files={len(parsed.files)}".strip(),
                           f"coverage-{parsed.adapter}", b"")
        ev.id = stable_id("ev", f"coverage:{run.id}", "coverage-import")
        store.upsert_evidence([ev])
        map_stats = cov_map.map_run(store, run.id, repo_id)
        link_stats = cov_map.link_tests(store, run.id, repo_id)
        fresh = cov_map.refresh_freshness(store, repo_id, repo_root_s)
        if _scope.get("record_identity"):
            _record_scope(store, _scope["roots"])
        store.commit()
    except Exception:
        try:
            store.rollback()
        except Exception:
            pass
        raise
    return {"run_id": run.id, "status": run.status, "revision_status": status,
            "files": len(parsed.files), "lines": len(parsed.lines),
            "branches": len(parsed.branches), "funcs": len(parsed.funcs),
            "contexts": len(parsed.contexts), "unmapped_dropped": unmapped,
            "mapping": map_stats, "test_links": link_stats, "freshness": fresh,
            "warnings": parsed.warnings[:10], "seconds": round(time.time() - t0, 3)}


def refresh_repo_coverage(store, repo_id: str, repo_root: str,
                          changed: list[str] | None = None,
                          commit: bool = True) -> dict:
    """After source re-indexing: recompute derived maps for changed files and
    refresh freshness. Raw runs are never deleted here."""
    from . import mapping as _m
    out = {"remapped": 0, "freshness": []}
    try:
        runs = [r["id"] for r in store.cx.execute(
            "SELECT id FROM coverage_runs WHERE repo_id=?", (repo_id,)).fetchall()]
    except Exception:
        return out
    for rid in runs:
        if changed:
            store.cx.execute(
                "DELETE FROM coverage_symbol_map WHERE run_id=? AND file IN (%s)" %
                ",".join("?" * len(changed)), (rid, *changed))
        stats = _m.map_run(store, rid, repo_id)
        out["remapped"] += stats["mapped"] + stats["ambiguous"] + stats["file_only"]
    out["freshness"] = _m.refresh_freshness(store, repo_id, str(Path(repo_root).resolve()))
    if commit:
        try:
            store.commit()
        except Exception:
            pass
    return out
