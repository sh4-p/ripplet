"""Test-plan + test-run persistence (Phase 10 Stage 6).

Additive tables on the existing SQLite database, same conventions as
`coverage/store.py` (CREATE IF NOT EXISTS, caller-owned transactions).
Run history and plans survive re-indexing: the orphan sweep only deletes
nodes/edges/evidence rows, never these tables. Explicit retention policies
prune oldest-first per repository (log files removed with their rows).
"""
from __future__ import annotations
import json
import time
from pathlib import Path

SCHEMA_ADD = """
CREATE TABLE IF NOT EXISTS test_plans(plan_id TEXT PRIMARY KEY, repo_id TEXT, project_id TEXT, mode TEXT, revision TEXT, digest TEXT, created_at REAL, plan_json TEXT);
CREATE INDEX IF NOT EXISTS idx_test_plans_repo ON test_plans(repo_id, created_at);
CREATE TABLE IF NOT EXISTS test_runs(id TEXT PRIMARY KEY, plan_id TEXT, repo_id TEXT, project_id TEXT, source_revision TEXT, source_fingerprint TEXT, runner TEXT, runner_version TEXT, selection TEXT, timeout_s REAL, max_output_bytes INT, coverage_enabled INT, coverage_run_id TEXT, coverage_attribution TEXT, started_at REAL, ended_at REAL, duration_ms REAL, exit_code TEXT, outcome TEXT, parse_status TEXT, collected INT, passed INT, failed INT, skipped INT, unreported INT, per_test TEXT, output_bytes INT, output_truncated INT, log_path TEXT, authorization TEXT, config_fps TEXT, warnings TEXT, error TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_test_runs_repo ON test_runs(repo_id, created_at);
CREATE INDEX IF NOT EXISTS idx_test_runs_plan ON test_runs(plan_id);
"""


def ensure_schema(store) -> None:
    store.cx.executescript(SCHEMA_ADD)


def _j(v) -> str:
    return json.dumps(v, default=str)


# ---- plans ----
def save_plan(store, plan: dict, project_id: str = "default",
              keep: int = 200) -> str:
    ensure_schema(store)
    store.cx.execute(
        "INSERT OR REPLACE INTO test_plans VALUES(?,?,?,?,?,?,?,?)",
        (plan["plan_id"], plan.get("repository", ""), project_id,
         plan.get("mode", ""), plan.get("revision", ""),
         plan.get("plan_id", ""), time.time(), _j(plan)))
    prune_plans(store, plan.get("repository", ""), keep)
    return plan["plan_id"]


def get_plan(store, plan_id: str) -> dict | None:
    ensure_schema(store)
    r = store.cx.execute("SELECT * FROM test_plans WHERE plan_id=?",
                         (plan_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["plan"] = json.loads(d.pop("plan_json") or "{}")
    except Exception:
        d["plan"] = {}
    return d


def list_plans(store, repo_id: str = "", limit: int = 50) -> list[dict]:
    ensure_schema(store)
    q = "SELECT plan_id, repo_id, mode, revision, created_at FROM test_plans" + \
        (" WHERE repo_id=?" if repo_id else "") + " ORDER BY created_at DESC LIMIT ?"
    args = ((repo_id, limit) if repo_id else (limit,))
    return [dict(r) for r in store.cx.execute(q, args).fetchall()]


def prune_plans(store, repo_id: str, keep: int) -> int:
    if keep < 0:
        return 0
    rows = store.cx.execute(
        "SELECT plan_id FROM test_plans WHERE repo_id=? ORDER BY created_at DESC",
        (repo_id,)).fetchall()
    old = [r["plan_id"] for r in rows[keep:]] if keep is not None else []
    for pid in old:
        store.cx.execute("DELETE FROM test_plans WHERE plan_id=?", (pid,))
    return len(old)


# ---- runs ----
def _log_dir(store) -> Path:
    try:
        base = Path(getattr(store, "path", ".")).parent
    except Exception:
        base = Path(".")
    d = base / "testruns"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_log(store, run_id: str, text: str) -> str:
    try:
        p = _log_dir(store) / f"{run_id}.log"
        p.write_text(text or "", encoding="utf-8", errors="replace")
        return str(p)
    except OSError:
        return ""


def save_run(store, rec, log_text: str = "", keep: int = 100) -> str:
    from .models import TestRunRecord
    ensure_schema(store)
    if not isinstance(rec, TestRunRecord):
        raise ValueError("save_run needs a TestRunRecord")
    log_path = write_log(store, rec.id, log_text) if log_text else ""
    if log_path:
        rec.log_path = log_path
    store.cx.execute(
        "INSERT OR REPLACE INTO test_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec.id, rec.plan_id, rec.repo_id, rec.project_id, rec.source_revision,
         _j(rec.source_fingerprint), rec.runner, rec.runner_version, _j(rec.selection),
         rec.timeout_s, rec.max_output_bytes, int(rec.coverage_enabled),
         rec.coverage_run_id, rec.coverage_attribution, rec.started_at, rec.ended_at,
         rec.duration_ms, _j(rec.exit_code), rec.outcome, rec.parse_status,
         rec.collected, rec.passed, rec.failed, rec.skipped, rec.unreported,
         _j(rec.per_test), rec.output_bytes, int(rec.output_truncated),
         rec.log_path, _j(rec.authorization), _j(rec.config_fps),
         _j(rec.warnings), rec.error, rec.created_at))
    prune_runs(store, rec.repo_id, keep)
    return rec.id


def get_run(store, run_id: str, with_log_excerpt: int = 0) -> dict | None:
    ensure_schema(store)
    r = store.cx.execute("SELECT * FROM test_runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("source_fingerprint", "selection", "per_test", "authorization",
              "config_fps", "warnings"):
        try:
            d[k] = json.loads(d.get(k) or ("[]" if k in ("selection", "per_test", "warnings") else "{}"))
        except Exception:
            d[k] = [] if k in ("selection", "per_test", "warnings") else {}
    try:
        d["exit_code"] = json.loads(d.get("exit_code") or "null")
    except Exception:
        pass
    if with_log_excerpt and d.get("log_path"):
        try:
            # Bounded read: one byte past the excerpt decides truncation, the
            # rest of a (possibly 100MB-capped) log is never loaded.
            with open(d["log_path"], "rb") as f:
                raw = f.read(with_log_excerpt + 1)
            text = raw.decode("utf-8", errors="replace")
            if len(raw) > with_log_excerpt:
                # Never split a multibyte character at the boundary.
                text = raw[:with_log_excerpt].decode("utf-8", errors="replace")
                d["log_excerpt"] = text
                d["log_truncated"] = True
            else:
                d["log_excerpt"] = text
                d["log_truncated"] = False
        except OSError:
            d["log_excerpt"] = ""
            d["log_truncated"] = False
    return d


def list_runs(store, repo_id: str = "", limit: int = 50) -> list[dict]:
    ensure_schema(store)
    q = ("SELECT id, plan_id, repo_id, runner, outcome, exit_code, collected, passed,"
         " failed, skipped, unreported, duration_ms, coverage_run_id, created_at"
         " FROM test_runs" + (" WHERE repo_id=?" if repo_id else "") +
         " ORDER BY created_at DESC LIMIT ?")
    args = ((repo_id, limit) if repo_id else (limit,))
    rows = []
    for r in store.cx.execute(q, args).fetchall():
        d = dict(r)
        try:
            d["exit_code"] = json.loads(d.get("exit_code") or "null")
        except Exception:
            pass
        rows.append(d)
    return rows


def prune_runs(store, repo_id: str, keep: int) -> int:
    if keep is None or keep < 0:
        return 0
    rows = store.cx.execute(
        "SELECT id, log_path FROM test_runs WHERE repo_id=? ORDER BY created_at DESC",
        (repo_id,)).fetchall()
    old = list(rows[keep:])
    for r in old:
        store.cx.execute("DELETE FROM test_runs WHERE id=?", (r["id"],))
        if r["log_path"]:
            try:
                Path(r["log_path"]).unlink(missing_ok=True)
            except OSError:
                pass
    return len(old)
