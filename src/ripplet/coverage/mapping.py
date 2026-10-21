"""Source revision verification and coverage-to-symbol mapping (Phase 8 §3/5).

Mapping is range-based over stored symbol spans, never by bare name. Every
association records its method and confidence; ties become `ambiguous`,
unmappable lines stay file-level. Raw runs are immutable; only derived
`coverage_symbol_map` rows are recomputed, and run freshness lives in a
separate state table so history survives source updates.
"""
from __future__ import annotations
import hashlib
import subprocess
from pathlib import Path

RELIABLE_LANGUAGES = ("python", "javascript", "typescript", "tsx")


def normalize_report_path(raw: str, repo_root: str) -> str | None:
    """Map one report path to a repo-relative path, or None when unsafe.

    Accepts repo-relative, absolute-in-root (incl. Windows drives), and ./
    forms. Rejects traversal outside the root, absolute paths elsewhere,
    NUL bytes and empty results. Never touches the filesystem beyond checks.
    """
    if not raw or "\x00" in raw:
        return None
    s = str(raw).replace("\\", "/").strip()
    if not s or s in (".", "/"):
        return None
    root = Path(repo_root).resolve()
    candidates: list[Path] = []
    p = Path(s)
    if p.is_absolute():
        candidates.append(p)
    else:
        if len(s) > 2 and s[1] == ":":
            return None  # foreign drive-letter path
        cleaned = s[2:] if s.startswith("./") else s
        candidates.append(root / cleaned)
    for cand in candidates:
        try:
            rel = cand.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        rel_s = str(rel).replace("\\", "/")
        if rel_s.startswith("..") or rel_s == ".":
            continue
        return rel_s
    return None


def git_head(repo_root: str) -> str:
    try:
        r = subprocess.run(["git", "-C", repo_root, "rev-parse", "HEAD"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _git_object_is_commit(repo_root: str, rev: str) -> bool:
    """True when rev names an existing git commit object (local check only).

    Used to distinguish `historical` (a real past commit) from `unverified`
    (unknown rev). Never raises; empty/non-commit/unknown all mean False.
    """
    if not rev or len(rev) > 100 or not all(
            c in "0123456789abcdefABCDEF" for c in rev.strip()):
        # Full/short hex SHAs only: branch names, tags and paths are not
        # revision evidence here (they move; a report citing them proves
        # nothing about content).
        return False
    try:
        r = subprocess.run(["git", "-C", repo_root, "cat-file", "-t", rev.strip()], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0 and r.stdout.strip() == "commit"
    except Exception:
        return False


def verify_revision(run: dict, repo_root: str, user_rev: str = "",
                    confirm_current: bool = False) -> tuple[str, dict]:
    """Return (status, notes). Never invents revision identity.

    verified: report/user rev matches git HEAD, or the user confirmed current
      (override is recorded, not silent). historical: the claimed rev is a
      real (non-HEAD) commit in this repo's history — usable as past evidence,
      never as current. unverified otherwise. Empty repo (no git) with no rev
      claims -> unverified.
    """
    notes: dict = {}
    claimed = (user_rev or "").strip() or (run.get("git_rev") or "").strip()
    head = git_head(repo_root)
    notes["git_head"] = head[:12] if head else ""
    notes["claimed"] = claimed[:12] if claimed else ""
    if confirm_current:
        notes["override"] = True
        return "override", notes
    if claimed and head and claimed == head:
        return "verified", notes
    if claimed and head and claimed != head and _git_object_is_commit(repo_root, claimed):
        notes["historical_commit"] = True
        return "historical", notes
    if claimed and not head:
        return "unverified", notes
    return "unverified", notes


def file_fingerprint(repo_root: str, rel: str) -> str:
    try:
        h = hashlib.sha256()
        with open(Path(repo_root) / rel, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except OSError:
        return ""


def candidate_symbols(store, repo_id: str, rel: str) -> list:
    """Mappable symbols of one file: reliable spans only.

    Generic-text nodes carry a single-line pseudo-span with no real range
    evidence, so they are file-level only (never narrowest-enclosing).
    """
    out = []
    rows = store.cx.execute(
        "SELECT * FROM nodes WHERE repository_id=? AND file_path=?", (repo_id, rel))
    from ..storage.sqlite_store import _row_node
    for r in rows:
        try:
            n = _row_node(r)
        except Exception:
            continue
        if n.type not in ("function", "method", "class", "interface", "type",
                          "enum", "variable"):
            continue
        loc = n.location
        try:
            span = (loc.end_line or loc.start_line) - (loc.start_line or 1)
        except Exception:
            span = 0
        reliable = (n.language in RELIABLE_LANGUAGES) and span >= 0 and \
            not (n.language == "text")
        out.append((n, span if reliable else None))
    return out


def map_line(store, repo_id: str, rel: str, line: int,
             symbols: list | None = None) -> tuple[list[tuple[str, str]], str]:
    """Map one covered line -> [(symbol_id, method)]. Narrowest enclosing wins;
    ties and unreliable spans stay file-level/ambiguous, never guessed."""
    if symbols is None:
        symbols = candidate_symbols(store, repo_id, rel)
    hits: list[tuple[str, int, str]] = []
    for n, span in symbols:
        if span is None:
            continue
        loc = n.location
        try:
            if (loc.start_line or 1) <= line <= (loc.end_line or loc.start_line or 1):
                hits.append((n.id, span, n.qualified_name or n.label))
        except Exception:
            continue
    if not hits:
        # file-level fallback only when the file itself is indexed
        frow = store.cx.execute(
            "SELECT id FROM nodes WHERE repository_id=? AND file_path=? AND type='file'",
            (repo_id, rel)).fetchone()
        if frow:
            return [frow["id"]], "file_only"
        return [], "unmapped"
    best = min(h[1] for h in hits)
    winners = sorted(h[0] for h in hits if h[1] == best)
    if len(winners) == 1:
        return winners, "narrowest"
    return winners, "ambiguous"


def map_run(store, run_id: str, repo_id: str) -> dict:
    """(Re)build derived symbol mappings for one run. Raw observations untouched.

    Returns stats; deletes only this run's map rows first (idempotent).
    """
    store.cx.execute("DELETE FROM coverage_symbol_map WHERE run_id=?", (run_id,))
    try:
        files = [r["file"] for r in store.cx.execute(
            "SELECT DISTINCT file FROM coverage_lines WHERE run_id=?", (run_id,)).fetchall()]
    except Exception:
        return {"mapped": 0, "ambiguous": 0, "file_only": 0, "unmapped": 0}
    stats = {"mapped": 0, "ambiguous": 0, "file_only": 0, "unmapped": 0}
    cache: dict[str, list] = {}
    for rel in sorted(set(files)):
        if rel not in cache:
            cache[rel] = candidate_symbols(store, repo_id, rel)
        for row in store.cx.execute(
                "SELECT DISTINCT line FROM coverage_lines WHERE run_id=? AND file=?",
                (run_id, rel)).fetchall():
            sids, method = map_line(store, repo_id, rel, row["line"], cache[rel])
            if method == "narrowest":
                stats["mapped"] += 1
            elif method == "ambiguous":
                stats["ambiguous"] += 1
            elif method == "file_only":
                stats["file_only"] += 1
            else:
                stats["unmapped"] += 1
            for sid in sids:
                store.cx.execute(
                    "INSERT OR IGNORE INTO coverage_symbol_map VALUES(?,?,?,?,?,?)",
                    (run_id, rel, row["line"], sid, "range", method))
    return stats


def _test_symbols(store, repo_id: str) -> list:
    """Test-identity candidates: explicit test type, test_ prefix, or test files.
    Returns node objects; ambiguity is resolved by callers, never here."""
    out = []
    try:
        rows = store.cx.execute("SELECT * FROM nodes WHERE repository_id=?",
                                (repo_id,)).fetchall()
    except Exception:
        return out
    from ..storage.sqlite_store import _row_node
    for r in rows:
        try:
            n = _row_node(r)
        except Exception:
            continue
        label = n.label or ""
        if n.type == "test" or label.startswith("test_") or label.endswith("_test") \
                or "/test_" in (n.file_path or "") or "/tests/" in (n.file_path or ""):
            out.append(n)
    return out


def link_tests(store, run_id: str, repo_id: str) -> dict:
    """Optional observed `test_covers_symbol` links (Phase 8 Stage 4).

    Created ONLY when a context label identifies exactly one test symbol AND
    that context's lines map to exactly one code symbol. Aggregate reports
    (no contexts) and ambiguous labels produce no links — verified by tests.
    """
    from ..core.identifiers import canonical_edge_id
    from ..core.models import Edge
    stats = {"links": 0, "ambiguous": 0, "unresolved": 0}
    try:
        ctx_rows = store.cx.execute(
            "SELECT DISTINCT context FROM coverage_contexts WHERE run_id=?",
            (run_id,)).fetchall()
    except Exception:
        return stats
    if not ctx_rows:
        return stats  # aggregate report: no per-test evidence, no links
    tests = _test_symbols(store, repo_id)
    by_label: dict[str, list] = {}
    for t in tests:
        by_label.setdefault(t.label, []).append(t)
        short = t.label.split("::")[-1]
        by_label.setdefault(short, []).append(t)
    fkey = f"coverage:{run_id}"
    for row in ctx_rows:
        label = row["context"]
        cands = sorted({t.id: t for t in (by_label.get(label, []) +
                                          by_label.get(label.split("::")[-1], []))}.values(),
                       key=lambda n: n.id)
        if len(cands) != 1:
            stats["ambiguous" if cands else "unresolved"] += 1
            continue
        test_node = cands[0]
        targets: set[str] = set()
        for crow in store.cx.execute(
                "SELECT file, line FROM coverage_contexts WHERE run_id=? AND context=?",
                (run_id, label)).fetchall():
            sids, method = map_line(store, repo_id, crow["file"], crow["line"])
            if method == "narrowest":
                targets.update(sids)
        if len(targets) != 1:
            stats["ambiguous" if targets else "unresolved"] += 1
            continue
        target = next(iter(targets))
        eid = canonical_edge_id(test_node.id, "test_covers_symbol", target, fkey)
        from ..core.identifiers import stable_id
        prov = [stable_id("ev", f"coverage:{run_id}", "coverage-import")]
        store.cx.execute(
            "INSERT OR REPLACE INTO edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, test_node.id, target, "test_covers_symbol", "observed", "observed",
             0.9, __import__("json").dumps(prov), "coverage-test-link", "1",
             "", None, __import__("json").dumps({"run_id": run_id, "context": label}),
             "", 0, 0))
        store.cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (fkey, eid))
        stats["links"] += 1
    return stats


def refresh_freshness(store, repo_id: str, repo_root: str) -> list[dict]:
    """Recompute per-run freshness after source changes. Run rows immutable;
    results go to an advisory state table (created lazily)."""
    store.cx.execute(
        "CREATE TABLE IF NOT EXISTS coverage_run_state(run_id TEXT PRIMARY KEY,"
        " freshness TEXT, checked_at REAL, stale_files TEXT)")
    out = []
    try:
        runs = [dict(r) for r in store.cx.execute(
            "SELECT * FROM coverage_runs WHERE repo_id=?", (repo_id,)).fetchall()]
    except Exception:
        return out
    import json
    import time
    for run in runs:
        try:
            fps = json.loads(run.get("source_fps") or "{}")
        except Exception:
            fps = {}
        stale = []
        for rel, sha in fps.items():
            if not sha:
                continue
            # Any fingerprint mismatch marks stale — including deleted files
            # (unreadable reads as ""), which previously slipped through and
            # left the run looking current after its evidence vanished.
            if file_fingerprint(repo_root, rel) != sha:
                stale.append(rel)
        stale = sorted(stale)
        freshness = "current" if not stale else "stale"
        if not fps:
            freshness = "unknown"
        store.cx.execute("INSERT OR REPLACE INTO coverage_run_state VALUES(?,?,?,?)",
                         (run["id"], freshness, time.time(), json.dumps(stale)))
        out.append({"run_id": run["id"], "freshness": freshness, "stale_files": stale})
    return out
