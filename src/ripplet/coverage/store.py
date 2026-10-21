"""Coverage persistence: additive tables on the existing SQLite database.

Same conventions as the graph store (WAL, one-transaction import, busy
timeout inherited from the connection). Coverage rows live in their own
tables; the orphan sweep never touches them, and source re-indexing never
deletes runs — only derived `coverage_symbol_map` rows are recomputed.
"""
from __future__ import annotations
import json
import time

SCHEMA_ADD = """
CREATE TABLE IF NOT EXISTS coverage_runs(id TEXT PRIMARY KEY, repo_id TEXT, project_id TEXT, tool TEXT, tool_version TEXT, format TEXT, format_version TEXT, report_hash TEXT, generated_at REAL, imported_at REAL, git_rev TEXT, git_branch TEXT, revision_status TEXT, source_fps TEXT, has_line INT, has_func INT, has_branch INT, has_contexts INT, label TEXT, meta TEXT, status TEXT, diagnostics TEXT);
CREATE INDEX IF NOT EXISTS idx_cov_runs_repo ON coverage_runs(repo_id);
CREATE TABLE IF NOT EXISTS coverage_files(run_id TEXT, file TEXT, repo_id TEXT, statements_found INT, statements_covered INT, lines_hit INT, lines_missed INT, funcs_found INT, funcs_covered INT, branches_found INT, branches_taken INT, match_status TEXT, PRIMARY KEY(run_id, file));
CREATE INDEX IF NOT EXISTS idx_cov_files_run ON coverage_files(run_id);
CREATE TABLE IF NOT EXISTS coverage_lines(run_id TEXT, file TEXT, line INT, hits INT, PRIMARY KEY(run_id, file, line));
CREATE INDEX IF NOT EXISTS idx_cov_lines_run ON coverage_lines(run_id);
CREATE TABLE IF NOT EXISTS coverage_branches(run_id TEXT, file TEXT, line INT, block INT, branch INT, taken INT, PRIMARY KEY(run_id, file, line, block, branch));
CREATE TABLE IF NOT EXISTS coverage_functions(run_id TEXT, file TEXT, name TEXT, line INT, hits INT, PRIMARY KEY(run_id, file, name));
CREATE TABLE IF NOT EXISTS coverage_contexts(run_id TEXT, context TEXT, file TEXT, line INT, hits INT);
CREATE TABLE IF NOT EXISTS coverage_symbol_map(run_id TEXT, file TEXT, line INT, symbol_id TEXT, method TEXT, confidence TEXT, PRIMARY KEY(run_id, file, line, symbol_id));
CREATE INDEX IF NOT EXISTS idx_cov_map_run ON coverage_symbol_map(run_id);
CREATE INDEX IF NOT EXISTS idx_cov_map_sym ON coverage_symbol_map(symbol_id);
"""


def ensure_schema(store) -> None:
    store.cx.executescript(SCHEMA_ADD)


def _j(v) -> str:
    return json.dumps(v or {})


def write_run(store, parsed, repo_id: str, project_id: str) -> str:
    """Persist one parsed report transactionally. Returns the run id.

    Deterministic idempotence: the same report bytes + repo + label always map
    to the same run id, so a repeated import refreshes instead of duplicating.
    Independent runs stay independent (different bytes/label => different id).
    """
    from .models import CoverageRun
    ensure_schema(store)
    run = parsed.run
    run.repo_id = repo_id
    run.project_id = project_id
    run.imported_at = time.time()
    store.cx.execute("DELETE FROM coverage_lines WHERE run_id=?", (run.id,))
    store.cx.execute("DELETE FROM coverage_branches WHERE run_id=?", (run.id,))
    store.cx.execute("DELETE FROM coverage_functions WHERE run_id=?", (run.id,))
    store.cx.execute("DELETE FROM coverage_contexts WHERE run_id=?", (run.id,))
    store.cx.execute("DELETE FROM coverage_files WHERE run_id=?", (run.id,))
    store.cx.execute("DELETE FROM coverage_symbol_map WHERE run_id=?", (run.id,))
    store.cx.execute(
        "INSERT OR REPLACE INTO coverage_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run.id, run.repo_id, run.project_id, run.tool, run.tool_version, run.format,
         run.format_version, run.report_hash, run.generated_at, run.imported_at,
         run.git_rev, run.git_branch, run.revision_status, _j(run.source_fps),
         int(run.has_line), int(run.has_func), int(run.has_branch), int(run.has_contexts),
         run.label, _j(run.meta), run.status, _j(run.diagnostics)))
    for f in parsed.files:
        store.cx.execute(
            "INSERT OR REPLACE INTO coverage_files VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run.id, f.file, repo_id, f.statements_found, f.statements_covered,
             f.lines_hit, f.lines_missed, f.funcs_found, f.funcs_covered,
             f.branches_found, f.branches_taken, f.match_status))
    store.cx.executemany("INSERT OR REPLACE INTO coverage_lines VALUES(?,?,?,?)",
                         [(run.id, ln.file, ln.line, ln.hits) for ln in parsed.lines])
    store.cx.executemany("INSERT OR REPLACE INTO coverage_branches VALUES(?,?,?,?,?,?)",
                         [(run.id, b.file, b.line, b.block, b.branch, b.taken)
                          for b in parsed.branches])
    store.cx.executemany("INSERT OR REPLACE INTO coverage_functions VALUES(?,?,?,?,?)",
                         [(run.id, fn.file, fn.name, fn.line, fn.hits)
                          for fn in parsed.funcs])
    store.cx.executemany("INSERT INTO coverage_contexts VALUES(?,?,?,?,?)",
                         [(run.id, cx.context, cx.file, cx.line, cx.hits)
                          for cx in parsed.contexts])
    return run.id


def list_runs(store, repo_id: str = "") -> list[dict]:
    ensure_schema(store)
    q = "SELECT * FROM coverage_runs" + (" WHERE repo_id=?" if repo_id else "") + \
        " ORDER BY imported_at DESC"
    args = (repo_id,) if repo_id else ()
    return [dict(r) for r in store.cx.execute(q, args).fetchall()]


def get_run(store, run_id: str) -> dict | None:
    ensure_schema(store)
    r = store.cx.execute("SELECT * FROM coverage_runs WHERE id=?", (run_id,)).fetchone()
    return dict(r) if r else None
