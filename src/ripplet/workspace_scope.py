"""Workspace/storage scope validation (Phase 12 Priority 0).

Single identity rule: every indexing operation binds an explicit set of
workspace roots to exactly one database. The database records the roots it
was built from; any later mutation whose roots disagree is rejected BEFORE
any write transaction, with an actionable error. Reads never require scope.

Identity record: meta key ``workspace_roots`` (JSON list of resolved root
paths) plus ``workspace_identity_v``. Databases written before this
mechanism carry no record ("legacy"); a legacy database adopts the current
roots only when every repository already stored in it is covered by the
current run (nothing foreign), otherwise it is rejected like any mismatch.

Path normalization is ``Path.resolve()`` only (matching the historical
repo_id derivation); distinct spellings of one directory (short/long names,
symlink variants) remain distinct identities and are reported as such.
"""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .core.errors import RippletError

IDENTITY_KEY = "workspace_roots"
IDENTITY_VERSION_KEY = "workspace_identity_v"
IDENTITY_VERSION = "1"


class WorkspaceMismatchError(RippletError):
    """Raised before mutation when roots/storage do not match the database."""


def repo_id_for(root: str) -> str:
    """Canonical repository identity. Same formula as service/testrun/
    coverage/cli/testplan: ``repo-`` + sha256(resolved root)[:10]."""
    return "repo-" + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:10]


def normalize_root(path: str) -> str:
    """Resolve one workspace root; it must exist and be a directory."""
    if not path:
        raise WorkspaceMismatchError(
            "no workspace root configured; run 'ripplet init <dir>' first")
    rp = str(Path(path).resolve())
    if not os.path.isdir(rp):
        raise WorkspaceMismatchError(
            f"workspace root is not an accessible directory: {path}\n"
            f"  resolved: {rp}")
    return rp


def normalize_storage(path: str) -> str:
    if not path:
        raise WorkspaceMismatchError("no storage path configured")
    return str(Path(path).resolve())


def normalize_roots(roots: list[str]) -> list[str]:
    if not roots:
        raise WorkspaceMismatchError(
            "no workspace roots configured; run 'ripplet init <dir>' first")
    out: list[str] = []
    for r in roots:
        nr = normalize_root(r)
        if nr not in out:
            out.append(nr)
    return out


def missing_roots(roots: list[str]) -> list[str]:
    """Configured roots that no longer exist (pure probe, never raises).

    A run whose roots are gone cannot write anything: the historical
    incomplete-scan path (``IncompleteScanError``, failed generation, last
    valid graph preserved) owns that failure. Callers skip the scope
    identity gate when this is non-empty so the pinned recovery contract
    keeps holding instead of being shadowed by a scope error.
    """
    missing = []
    for r in (roots or []):
        try:
            rp = str(Path(r).resolve())
        except Exception:
            missing.append(str(r))
            continue
        if not os.path.isdir(rp):
            missing.append(str(r))
    return missing


def read_db_workspace(db_path: str) -> dict | None:
    """Read-only workspace identity probe. Returns None for fresh/legacy DBs.

    Never creates files or writes rows: opens with ``mode=ro``. A missing
    file, an unreadable-but-empty schema, or absent identity keys all mean
    "no recorded identity" (fresh vs legacy is decided by content checks).
    """
    try:
        cx = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except Exception:
        return None
    try:
        cx.row_factory = sqlite3.Row
        try:
            row = cx.execute(
                "SELECT v FROM meta WHERE k=?", (IDENTITY_KEY,)).fetchone()
        except Exception:
            return None  # no meta table yet: fresh database
        if row is None:
            return None  # legacy database: content check decides
        try:
            roots = json.loads(row["v"])
        except Exception:
            return None
        ver = None
        try:
            vr = cx.execute("SELECT v FROM meta WHERE k=?",
                            (IDENTITY_VERSION_KEY,)).fetchone()
            ver = vr["v"] if vr else None
        except Exception:
            pass
        return {"roots": list(roots), "version": ver}
    finally:
        try:
            cx.close()
        except Exception:
            pass


def db_repo_ids(db_path: str) -> set[str]:
    """Repository ids with stored file rows. Empty set for fresh DBs."""
    try:
        cx = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except Exception:
        return set()
    try:
        cx.row_factory = sqlite3.Row
        try:
            rows = cx.execute("SELECT DISTINCT repo_id FROM files").fetchall()
        except Exception:
            return set()
        return {r["repo_id"] for r in rows if r["repo_id"]}
    finally:
        try:
            cx.close()
        except Exception:
            pass


def check_scope(*, roots: list[str], storage_path: str, intent: str = "index") -> dict:
    """Validate (roots, storage) against the database BEFORE any mutation.

    Returns ``{"roots", "repo_ids", "storage", "record_identity",
    "notice"}``. Raises :class:`WorkspaceMismatchError` on any incompatibility.
    Read-only: opens the database with ``mode=ro`` and never writes.
    """
    norm_roots = normalize_roots(roots)
    norm_storage = normalize_storage(storage_path)
    repo_ids = [repo_id_for(r) for r in norm_roots]
    identity = read_db_workspace(norm_storage)
    content_repos = db_repo_ids(norm_storage)
    notice = ""
    record = False
    if identity is None and not content_repos:
        # Fresh database: adopt on first successful write.
        record = True
    elif identity is not None:
        stored = list(identity.get("roots") or [])
        if set(norm_roots) == set(stored):
            pass
        elif set(stored) < set(norm_roots):
            # `init --link` added repositories: a superset keeps every stored
            # fact and records the wider identity. Dropping a root still
            # requires `workspace forget-repo` (rejected below).
            record = True
            notice = (f"workspace at {norm_storage} gains "
                      f"{sorted(set(norm_roots) - set(stored))}")
        else:
            raise WorkspaceMismatchError(_mismatch_message(
                intent, norm_storage, norm_roots, stored, content_repos))
    else:
        # Legacy database (content, no identity): adopt only when the current
        # run covers everything already stored; foreign content is rejected.
        if content_repos and content_repos <= set(repo_ids):
            record = True
            notice = (f"legacy database at {norm_storage} adopts workspace "
                      f"identity {norm_roots} (all {len(content_repos)} stored "
                      f"repositories covered by this run)")
        elif content_repos:
            raise WorkspaceMismatchError(_mismatch_message(
                intent, norm_storage, norm_roots, None, content_repos))
        else:
            record = True
    return {"roots": norm_roots, "repo_ids": repo_ids, "storage": norm_storage,
            "record_identity": record, "notice": notice}


def _mismatch_message(intent: str, storage: str, roots: list[str],
                      stored_roots: list[str] | None,
                      content_repos: set[str]) -> str:
    lines = [f"refusing {intent}: workspace/storage scope mismatch (no data changed).",
             f"  database : {storage}"]
    if stored_roots is not None:
        lines.append("  database workspace roots:")
        lines += [f"    - {r}" for r in stored_roots]
    elif content_repos:
        lines.append("  database holds repositories not covered by this run:")
        lines += [f"    - {r}" for r in sorted(content_repos)]
    lines.append("  requested workspace roots:")
    lines += [f"    - {r}" for r in roots]
    lines += ["",
              "  Fix one of these (then re-run):",
              "  - run from the indexed workspace so its own .ripplet/config.json applies,",
              "  - pass --config <workspace>/.ripplet/config.json for the intended workspace,",
              "  - pass --storage <path> for a separate database,",
              "  - run 'ripplet doctor' to inspect bindings, or",
              "  - run 'ripplet workspace forget-repo --repo <id> --confirm' to deliberately",
              "    remove one repository's facts before rebinding."]
    return "\n".join(lines)


def record_scope(store, roots: list[str]) -> None:
    """Persist workspace identity. Call once, after a successful mutation."""
    store.cx.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                     (IDENTITY_KEY, json.dumps(sorted(roots))))
    store.cx.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                     (IDENTITY_VERSION_KEY, IDENTITY_VERSION))


def diagnose_scope(cfg) -> dict:
    """Read-only scope diagnosis for `ripplet doctor` (never mutates)."""
    from pathlib import Path as _P
    roots_cfg = list(getattr(cfg, "workspace_roots", None) or [])
    storage_cfg = getattr(cfg, "storage_path", "")
    resolved_roots, root_errors = [], []
    for r in roots_cfg:
        try:
            resolved_roots.append(normalize_root(r))
        except WorkspaceMismatchError as e:
            root_errors.append(str(e).splitlines()[0])
    storage = None
    try:
        storage = normalize_storage(storage_cfg)
    except WorkspaceMismatchError as e:
        root_errors.append(str(e).splitlines()[0])
    identity = read_db_workspace(storage) if storage else None
    content = db_repo_ids(storage) if storage else set()
    verdict, problems = "ok", list(root_errors)
    if identity is not None and set(resolved_roots) != set(identity.get("roots") or []):
        verdict = "mismatch"
        problems.append("configured roots differ from the database workspace identity")
    elif identity is None and content:
        covered = {repo_id_for(r) for r in resolved_roots}
        foreign = sorted(content - covered)
        if foreign:
            verdict = "suspected-mixing"
            problems.append(f"database holds repositories outside the configured workspace: {foreign}")
    return {"cwd": str(_P.cwd()), "configured_roots": roots_cfg,
            "resolved_roots": resolved_roots, "storage_configured": storage_cfg,
            "storage_resolved": storage, "db_identity": identity,
            "db_content_repos": sorted(content), "verdict": verdict,
            "problems": problems}
