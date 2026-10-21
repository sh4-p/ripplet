"""Keep answers in step with the files the agent is editing.

Between two tool calls an agent usually edits code. Without a refresh the
next answer comes from the old index: shifted line numbers, a removed
function still listed, a new one missing -- exactly what makes a model act
on code that is no longer there. Before a read tool answers, this checks
whether any indexed file changed since the database was last written and,
if so, runs the (incremental) index first.

Cheap when nothing changed: one stat per indexed file, at most every
`_MIN_INTERVAL` seconds. Large change sets (a branch switch) are reported
instead of reindexed inline, so a call never stalls for minutes.
Set RIPPLET_AUTO_REFRESH=0 to disable.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

_MIN_INTERVAL = 1.5
_MAX_INLINE = 200          # changed files reindexed inline; more -> note only
_SKIP_TOOLS = {"check_change", "use_workspace", "index_workspace", "get_index_status",
               "get_stale_sources", "get_test_run", "list_test_runs"}
_last = {"t": 0.0, "db": "", "note": ""}


def _stamp(cfg) -> Path:
    return Path(str(getattr(cfg, "storage_path", "") or "graph.db") + ".fresh")


def _indexed_at(cfg) -> float:
    """When the index last caught up with the files. The database itself is
    not enough: in WAL mode its mtime lags the -wal file, and an index run
    that found nothing to change may write nothing at all."""
    base = str(getattr(cfg, "storage_path", "") or "")
    t = 0.0
    for cand in (base, base + "-wal", str(_stamp(cfg))):
        try:
            t = max(t, Path(cand).stat().st_mtime)
        except OSError:
            pass
    return t


def mark_fresh(cfg) -> None:
    try:
        st = _stamp(cfg)
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def changed_files(store, cfg) -> list:
    since = _indexed_at(cfg)
    if not since:
        return []
    import hashlib
    roots = {}
    for r in (getattr(cfg, "workspace_roots", None) or ["."]):
        rp = str(Path(r).resolve())
        roots["repo-" + hashlib.sha256(rp.encode()).hexdigest()[:10]] = Path(rp)
    try:
        rows = store.cx.execute("SELECT path FROM files").fetchall()
    except Exception:
        return []
    out = []
    for (key,) in rows:
        rid, _, rel = str(key).partition(":")
        root = roots.get(rid)
        if root is None or not rel:
            continue
        try:
            if (root / rel).stat().st_mtime > since + 0.5:
                out.append(rel)
        except OSError:
            out.append(rel)            # deleted since the index: also stale
    return out


def ensure_fresh(store, cfg, tool: str) -> str:
    """'' when the index is current (or was just refreshed), else a one-line
    note to put in front of the answer."""
    if tool in _SKIP_TOOLS or os.environ.get("RIPPLET_AUTO_REFRESH", "1") == "0":
        return ""
    now = time.time()
    key = str(getattr(cfg, "storage_path", ""))
    if _last["db"] == key and now - _last["t"] < _MIN_INTERVAL:
        return _last["note"]
    _last.update(t=now, db=key, note="")
    changed = changed_files(store, cfg)
    if not changed:
        return ""
    if len(changed) > _MAX_INLINE:
        note = (f"[ripplet: {len(changed)} files changed since the last index; answers may be "
                f"stale. Run `ripplet index` (or call check_change) to refresh.]")
        _last["note"] = note
        return note
    try:
        from ..service import index_workspace
        index_workspace(cfg)          # stamps freshness itself
    except Exception as e:  # never fail the call over a refresh
        note = f"[ripplet: could not refresh the index ({type(e).__name__}); answers may be stale.]"
        _last["note"] = note
        return note
    _last["t"] = time.time()
    return ""
