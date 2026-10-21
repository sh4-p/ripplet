"""Execution orchestration (Phase 10 Stages 2/4/5/7).

Flow: stored plan -> build_request (validate-only, no process) -> preview
(human summary) -> authorize (explicit stamp) -> execute (bounded subprocess,
redacted + persisted record, optional explicit coverage import).

Planning never calls this module. MCP never calls execute().
"""
from __future__ import annotations
import hashlib
import os
import sys
import time
from pathlib import Path

from .models import (ExecutionRequest, ExecutionRejected, SelectedTest,
                     TestRunRecord, SUPPORTED_RUNNERS, DEFAULTS)
from . import store as run_store


def repo_id_for(root: str) -> str:
    return "repo-" + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:10]


def fingerprint_files(repo_root: str, rels: list[str]) -> dict[str, str]:
    """sha256 per file; missing/unreadable -> '' (reported, never invented)."""
    out = {}
    base = Path(repo_root).resolve()
    for rel in sorted(set(rels)):
        try:
            p = (base / rel).resolve()
            try:
                p.relative_to(base)
            except ValueError:
                out[rel] = ""
                continue
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            out[rel] = "sha256:" + h.hexdigest()
        except OSError:
            out[rel] = ""
    return out


def _check_root(repo_root: str) -> str:
    if not repo_root:
        raise ExecutionRejected("no repository root configured")
    rr = str(Path(repo_root).resolve())
    if not Path(rr).is_dir():
        raise ExecutionRejected(f"repository root is not a directory: {repo_root}")
    return rr


def derive_selector(store, repo_id: str, repo_root: str, test_id: str) -> SelectedTest:
    """Map one graph test node to an exact pytest selector.

    test node -> `<file>::<Class>::<test>`; file/module node in a test path
    -> `<file>` (whole file scope, stated in the manifest). Anything else
    (non-test types, non-Python, missing files, scope-widening fallbacks) is
    rejected rather than guessed.
    """
    node = store.get_node(test_id)
    if node is None:
        raise ExecutionRejected(f"unknown test id: {test_id}")
    if (node.repository_id or "") != repo_id:
        raise ExecutionRejected(f"test id escapes repository scope: {test_id}")
    rel = (node.file_path or "").replace("\\", "/")
    if not rel or not rel.endswith(".py"):
        raise ExecutionRejected(f"test id is not a runnable Python test: {test_id}")
    ap = (Path(repo_root).resolve() / rel).resolve()
    try:
        ap.relative_to(Path(repo_root).resolve())
    except ValueError:
        raise ExecutionRejected(f"test path escapes repository root: {rel}")
    if not ap.is_file():
        raise ExecutionRejected(f"test file no longer exists: {rel}")
    lang = (node.language or "").lower()
    if lang and lang != "python":
        raise ExecutionRejected(f"test id is not a Python test ({lang}): {test_id}")
    ntype = node.type or ""
    if ntype == "test":
        mod = rel[:-3].replace("/", ".")
        qn = node.qualified_name or ""
        if not qn.startswith(mod + "."):
            raise ExecutionRejected(
                f"cannot derive an exact selector for {test_id} "
                f"(qualified name {qn!r} does not extend module {mod!r}); "
                "refusing to widen scope to the whole file")
        suffix = qn[len(mod) + 1:].replace(".", "::")
        if not suffix:
            raise ExecutionRejected(f"empty selector for {test_id}")
        selector = f"{rel}::{suffix}"
    elif ntype in ("file", "module"):
        selector = rel
    else:
        raise ExecutionRejected(
            f"test id is not an executable test scope (type {ntype}): {test_id}")
    return SelectedTest(test_id=node.id, selector=selector, file=rel,
                        display=node.qualified_name or node.label)


def _selection_files(plan: dict, subset: list[str] | None) -> list[dict]:
    cands = plan.get("candidates", [])
    if subset:
        wanted = set(subset)
        have = {c.get("test_id") for c in cands}
        missing = sorted(wanted - have)
        if missing:
            raise ExecutionRejected(
                f"selected tests are not in plan {plan.get('plan_id')}: "
                + ", ".join(missing[:10]))
        cands = [c for c in cands if c.get("test_id") in wanted]
    if not cands:
        raise ExecutionRejected("empty selection: nothing approved for execution")
    return cands


def build_request(store, cfg, plan_row: dict, tests_subset: list[str] | None = None,
                  runner: str = "pytest", timeout_s: float | None = None,
                  max_output_bytes: int | None = None,
                  max_tests: int | None = None,
                  coverage: bool = False, python_exe: str = "",
                  adapter=None) -> ExecutionRequest:
    """Validate-only construction. Starts no process except the runner probe
    (`python -m pytest --version`, which loads the installed runner package
    but collects nothing from the repository)."""
    from .adapters import get_adapter
    plan = plan_row.get("plan") or {}
    plan_id = plan_row.get("plan_id") or plan.get("plan_id", "")
    if not plan or not plan_id:
        raise ExecutionRejected("stored plan is empty or corrupt")
    if runner != "pytest":
        raise ExecutionRejected(
            f"unsupported runner: {runner!r} (supported: 'pytest'; "
            "jest/vitest/mocha are not implemented in this phase)")
    roots = getattr(cfg, "workspace_roots", None) or []
    if not roots:
        raise ExecutionRejected("no workspace; run init first")
    repo_root = _check_root(roots[0])
    rid = repo_id_for(repo_root)
    # Priority 0: the execution database must belong to this workspace.
    # A run record written into a foreign database is cross-contamination.
    from ..workspace_scope import check_scope, WorkspaceMismatchError
    try:
        check_scope(roots=[repo_root], storage_path=getattr(store, "path", ""),
                    intent="test-run")
    except WorkspaceMismatchError as e:
        raise ExecutionRejected(str(e).splitlines()[0] + " [repository scope]")
    if (plan.get("repository") or "") != rid:
        raise ExecutionRejected(
            f"plan {plan_id} belongs to repository {plan.get('repository')}, "
            f"not the current workspace ({rid})")
    cands = _selection_files(plan, tests_subset)
    timeout = float(timeout_s if timeout_s is not None
                    else getattr(cfg, "test_timeout_s", DEFAULTS["timeout_s"]))
    max_out = int(max_output_bytes if max_output_bytes is not None
                  else getattr(cfg, "test_max_output_bytes", DEFAULTS["max_output_bytes"]))
    max_n = int(max_tests if max_tests is not None
                else getattr(cfg, "test_max_tests", DEFAULTS["max_tests"]))
    if timeout <= 0 or timeout > 86400:
        raise ExecutionRejected(f"timeout out of range (0, 86400]: {timeout}")
    if max_out <= 0 or max_out > 100_000_000:
        raise ExecutionRejected(f"output limit out of range: {max_out}")
    if max_n <= 0 or max_n > 10000:
        raise ExecutionRejected(f"max tests out of range: {max_n}")
    if len(cands) > max_n:
        raise ExecutionRejected(
            f"selection has {len(cands)} tests, over the limit of {max_n}; "
            "narrow --tests or raise the limit explicitly")
    exe = python_exe or sys.executable
    if not exe or not Path(exe).is_file():
        raise ExecutionRejected(f"python executable not found: {exe!r}")
    adapter = adapter or get_adapter("pytest")
    probe = adapter.probe(exe)
    if not probe.get("ok"):
        raise ExecutionRejected(
            f"runner startup failed: pytest is not available for {exe} "
            f"({probe.get('detail', 'no detail')})")
    selection = [derive_selector(store, rid, repo_root, c.get("test_id", ""))
                 for c in cands]
    cov_tool = ""
    if coverage:
        cov = adapter.probe_cov(exe)
        if cov.get("ok"):
            cov_tool = "pytest-cov"
        else:
            cov_tool = ""
    files = [s.file for s in selection]
    files += [f.get("file", "") for f in plan.get("changed_files", [])]
    req = ExecutionRequest(
        repo_id=rid, repo_root=repo_root, plan_id=plan_id, selection=selection,
        runner="pytest", python_exe=exe, runner_version=str(probe.get("version", "")),
        timeout_s=timeout, max_output_bytes=max_out,
        coverage=bool(coverage and cov_tool), coverage_tool=cov_tool,
        source_revision=plan.get("revision", "") or "",
        source_fingerprint=fingerprint_files(repo_root, [f for f in files if f]))
    if coverage and not cov_tool:
        req.coverage = False
    return req


def preview_request(req: ExecutionRequest, adapter=None) -> str:
    from .adapters import get_adapter
    adapter = adapter or get_adapter(req.runner)
    try:
        argv = adapter.build_argv(req.python_exe,
                                  [s.selector for s in req.selection])
    except ExecutionRejected as e:
        argv = [f"<unbuildable: {e}>"]
    L = ["Test-run request (NOT started — confirmation required):",
         f"  repository : {req.repo_root} ({req.repo_id})",
         f"  plan       : {req.plan_id}",
         f"  runner     : {req.runner} {req.runner_version} via {req.python_exe}",
         f"  command    : {' '.join(argv)}",
         f"  tests      : {len(req.selection)}"]
    for s in req.selection[:20]:
        L.append(f"    - {s.selector}")
    if len(req.selection) > 20:
        L.append(f"    ... and {len(req.selection) - 20} more")
    L += [f"  timeout    : {req.timeout_s}s   output cap: {req.max_output_bytes} bytes",
          f"  revision   : {req.source_revision or '(unrecorded)'}",
          f"  coverage   : {'enabled (' + req.coverage_tool + ')' if req.coverage else 'disabled'}",
          "  WARNING    : this executes repository-controlled test code, fixtures,",
          "               plugins, and configuration. Subprocess isolation is NOT a sandbox."]
    return "\n".join(L)


def check_freshness(req: ExecutionRequest) -> dict:
    """Compare approval-time fingerprints with the current tree."""
    current = fingerprint_files(req.repo_root, list(req.source_fingerprint))
    changed = sorted(rel for rel, sha in req.source_fingerprint.items()
                     if current.get(rel) != sha)
    return {"ok": not changed, "changed": changed, "current": current}


def authorize(req: ExecutionRequest, method: str, by: str = "",
              reapproval: bool = False) -> ExecutionRequest:
    from .models import AUTH_METHODS
    if method not in AUTH_METHODS:
        raise ExecutionRejected(f"unknown authorization method: {method!r}")
    req.authorization = {"method": method, "by": by or "local-user",
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "reapproval": bool(reapproval),
                         "source_fingerprint": dict(req.source_fingerprint)}
    return req


def _lock_path(store, slot: int) -> Path:
    try:
        base = Path(getattr(store, "path", ".")).parent
    except Exception:
        base = Path(".")
    return base / f".testrun-{slot}.lock"


def acquire_slot(store, max_slots: int) -> tuple[int, Path]:
    """Take a concurrency slot (O_CREAT|O_EXCL). Stale locks (dead pid) are
    reclaimed; a live holder means 'busy', never a queue."""
    import time as _t
    for slot in range(max(1, max_slots)):
        lp = _lock_path(store, slot)
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()} {_t.time()}".encode())
            finally:
                os.close(fd)
            return slot, lp
        except FileExistsError:
            try:
                content = lp.read_text().strip().split()
                pid = int(content[0])
                alive = True
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
                except OSError:
                    alive = True
                if not alive:
                    try:
                        lp.unlink()
                    except OSError:
                        pass
                    else:
                        continue
            except (OSError, ValueError, IndexError):
                pass
    raise ExecutionRejected(
        f"another test execution is in progress (all {max_slots} slot(s) busy)")


def release_slot(lp: Path) -> None:
    try:
        lp.unlink(missing_ok=True)
    except OSError:
        pass


def _outcome_from_exit(parsed: dict, exit_code, timed_out: bool,
                       limit_hit: bool) -> tuple[str, str]:
    if timed_out:
        return "timed_out", parsed.get("parse_status", "partial")
    if exit_code == 0:
        if parsed.get("parse_status") == "ok":
            return "passed", "ok"
        if parsed.get("per_test"):
            return "partial", "partial"
        return "result_parse_error", "failed"
    if exit_code == 1:
        # tests ran and failed; per-test detail may still be unavailable
        if parsed.get("parse_status") in ("ok", "partial") and parsed.get("per_test"):
            return "failed", parsed["parse_status"]
        return "failed", "failed"
    if exit_code in (2, 3, 4):
        return "runner_error", parsed.get("parse_status", "failed")
    if exit_code == 5:
        return "runner_error", parsed.get("parse_status", "failed")
    return "runner_error", "failed"


def execute(req: ExecutionRequest, store, cfg, adapter=None,
            on_progress=None) -> TestRunRecord:
    """Run the approved request under bounds and persist the record.

    Requires req.authorized(). Re-checks source freshness (refuses on drift
    without reapproval). Never invents per-test outcomes. Optionally imports
    an explicitly-produced coverage report through the existing pipeline.
    """
    import hashlib as _hl
    from .adapters import get_adapter
    from ..security.sanitization import redact_secrets
    if not req.authorized():
        raise ExecutionRejected(
            "execution requires explicit authorization (no plan, session, or "
            "connection implies consent)")
    fresh = check_freshness(req)
    if not fresh["ok"] and not req.authorization.get("reapproval"):
        raise ExecutionRejected(
            "source tree changed after approval: " +
            ", ".join(fresh["changed"][:10]) +
            " — regenerate the plan or re-run with explicit reapproval")
    adapter = adapter or get_adapter(req.runner)
    max_slots = int(getattr(cfg, "test_max_concurrent",
                            DEFAULTS["max_concurrent_runs"]))
    slot, lp = acquire_slot(store, max_slots)
    run_id = "run:" + _hl.sha256(
        f"{req.plan_id}\x00{time.time()}\x00{os.getpid()}".encode()).hexdigest()[:12]
    rec = TestRunRecord(id=run_id, plan_id=req.plan_id, repo_id=req.repo_id,
                        project_id=getattr(cfg, "project_id", "default"),
                        source_revision=req.source_revision,
                        source_fingerprint=dict(req.source_fingerprint),
                        runner=req.runner, runner_version=req.runner_version,
                        selection=[s.to_dict() for s in req.selection],
                        timeout_s=req.timeout_s,
                        max_output_bytes=req.max_output_bytes,
                        coverage_enabled=req.coverage,
                        authorization=dict(req.authorization))
    try:
        try:
            cfg_fps = fingerprint_files(
                req.repo_root, ["pyproject.toml", "pytest.ini", "setup.cfg",
                                "tox.ini", "conftest.py", "tests/conftest.py"])
        except Exception:
            cfg_fps = {}
        rec.config_fps = {k: v for k, v in cfg_fps.items() if v}
        cov_report = ""
        if req.coverage:
            cov_report = str(Path(_cov_tmp(req)) )
        argv = adapter.build_argv(req.python_exe,
                                  [s.selector for s in req.selection],
                                  coverage_report=cov_report if req.coverage else "")
    except ExecutionRejected as e:
        rec.outcome, rec.parse_status, rec.error = "runner_error", "failed", str(e)
        rec.ended_at = rec.started_at = time.time()
        _persist(store, cfg, rec, "")
        release_slot(lp)
        return rec
    from . import process as _proc
    rec.started_at = time.time()
    outcome = "runner_error"
    raw: dict = {"returncode": None, "output": "", "output_bytes": 0,
                 "truncated": False, "limit_hit": False, "timed_out": False,
                 "term_steps": [], "duration_ms": 0.0}
    error = ""
    cancelled = False
    try:
        try:
            raw = _proc.run_bounded(argv, req.repo_root, req.timeout_s,
                                    req.max_output_bytes)
        except OSError as e:
            error = f"failed runner startup: {type(e).__name__}: {e}"
            raw = {"returncode": None, "output": "", "output_bytes": 0,
                   "truncated": False, "limit_hit": False, "timed_out": False,
                   "term_steps": [], "duration_ms": 0.0}
        except KeyboardInterrupt:
            cancelled = True
            error = "cancelled by user before completion"
    finally:
        release_slot(lp)
    rec.ended_at = time.time()
    rec.duration_ms = raw.get("duration_ms", 0.0) or \
        round((rec.ended_at - rec.started_at) * 1000, 1)
    redacted = redact_secrets(raw.get("output", ""))
    rec.output_bytes = len(redacted.encode("utf-8", "replace"))
    rec.output_truncated = bool(raw.get("truncated"))
    if cancelled:
        outcome, parse_status, parsed = "cancelled", "failed", {"per_test": []}
    else:
        parsed = adapter.parse_output(redacted, truncated=rec.output_truncated)
        parse_status = parsed.get("parse_status", "failed")
        if error and raw.get("returncode") is None and not parsed.get("per_test"):
            outcome = "runner_error"
        elif raw.get("limit_hit") and not parsed.get("per_test"):
            outcome = "result_parse_error"
            error = (error + " output limit exceeded before any result parsed").strip()
        else:
            outcome, parse_status = _outcome_from_exit(
                parsed, raw.get("returncode"), bool(raw.get("timed_out")),
                bool(raw.get("limit_hit")))
            if raw.get("limit_hit") and parse_status == "ok":
                parse_status = "partial"
    if raw.get("timed_out"):
        error = (error + " process exceeded timeout and was terminated").strip()
    if raw.get("term_steps"):
        rec.warnings.append("termination: " + ",".join(raw["term_steps"][:6]))
    counts = parsed.get("counts", {}) if not cancelled else {}
    summary = parsed.get("summary") or {}
    rec.exit_code = raw.get("returncode")
    rec.outcome, rec.parse_status = outcome, parse_status
    rec.collected = parsed.get("collected", 0) or 0
    if summary:
        # Summary line is authoritative for totals; per-test rows keep detail.
        rec.passed = int(summary.get("passed", 0) or 0)
        rec.failed = int(summary.get("failed", 0) or 0) + int(summary.get("error", 0) or 0)
        rec.skipped = int(summary.get("skipped", 0) or 0)
    elif not cancelled:
        rec.passed = int(counts.get("passed", 0) or 0)
        rec.failed = int(counts.get("failed", 0) or 0) + int(counts.get("error", 0) or 0)
        rec.skipped = int(counts.get("skipped", 0) or 0)
    reported = sum(1 for _ in parsed.get("per_test", []))
    rec.unreported = max(rec.collected - reported, 0)
    rec.per_test = [{"node": p.get("node", ""), "outcome": p.get("outcome", "unknown")}
                    for p in parsed.get("per_test", [])]
    if parsed.get("no_tests"):
        rec.warnings.append("runner reported no tests collected")
    if rec.output_truncated:
        rec.warnings.append(
            f"captured output hit the {req.max_output_bytes}-byte cap; "
            "individual outcomes may be unavailable")
    rec.error = error
    # Optional explicit coverage: import the produced report, link it, and
    # record its attribution honestly (per-test vs aggregate). Never invent.
    if req.coverage and not cancelled:
        _import_run_coverage(store, cfg, req, rec, cov_report)
    try:
        if cov_report:
            import shutil
            shutil.rmtree(str(Path(cov_report).parent), ignore_errors=True)
    except Exception:
        pass
    _persist(store, cfg, rec, redacted)
    return rec


def _cov_tmp(req: ExecutionRequest) -> str:
    import tempfile
    d = tempfile.mkdtemp(prefix="ripplet-cov-")
    return str(Path(d) / "cov.json")


def _import_run_coverage(store, cfg, req: ExecutionRequest, rec: TestRunRecord,
                         report_path: str) -> None:
    from ..coverage import service as _cov
    try:
        if not report_path or not Path(report_path).is_file():
            rec.warnings.append("coverage enabled but no report was produced; "
                                "no coverage ingested")
            return
        out = _cov.import_report(store, report_path, req.repo_root,
                                 label=f"testrun:{rec.id}",
                                 revision=req.source_revision or "",
                                 project_id=getattr(cfg, "project_id", "default"))
        rec.coverage_run_id = out.get("run_id", "")
        try:
            from ..coverage import store as _cs
            run = _cs.get_run(store, rec.coverage_run_id)
            has_ctx = bool(run and run.get("has_contexts"))
            rec.coverage_attribution = \
                "per-test" if has_ctx else "aggregate"
            if not has_ctx:
                rec.warnings.append(
                    "coverage report has no per-test contexts: aggregate "
                    "observations only, no test-to-symbol links inferred")
        except Exception:
            rec.coverage_attribution = "unknown"
    except Exception as e:
        rec.warnings.append(f"coverage import failed ({type(e).__name__}): {e}")


def _persist(store, cfg, rec: TestRunRecord, log_text: str) -> None:
    keep = int(getattr(cfg, "test_retention_runs", DEFAULTS["retention_runs"]))
    run_store.save_run(store, rec, log_text=log_text, keep=keep)
    _record_workspace_best_effort(store, cfg)
    try:
        store.commit()
    except Exception:
        pass


def _record_workspace_best_effort(store, cfg) -> None:
    """Bind legacy databases on first successful write. Never fails the op."""
    try:
        from ..workspace_scope import check_scope, record_scope
        roots = list(getattr(cfg, "workspace_roots", None) or [])
        if not roots or not getattr(store, "path", ""):
            return
        scope = check_scope(roots=roots, storage_path=store.path,
                            intent="record")
        if scope.get("record_identity"):
            record_scope(store, scope["roots"])
    except Exception:
        pass


def _persist_plan(store, cfg, plan: dict) -> None:
    keep = int(getattr(cfg, "test_plan_retention", DEFAULTS["retention_plans"]))
    run_store.save_plan(store, plan,
                        project_id=getattr(cfg, "project_id", "default"),
                        keep=keep)
    _record_workspace_best_effort(store, cfg)
    try:
        store.commit()
    except Exception:
        pass
