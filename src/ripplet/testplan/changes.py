"""Change inputs: diff/revisions/working-tree/files/symbols -> changed files
and symbols (Phase 9 Stage 3).

Git is used read-only through fixed argument arrays (no shell, no builds, no
test execution). A file that cannot be mapped to symbols keeps file-level
impact instead of being silently dropped. Deleted symbols are reported with
whatever dangling-reference evidence survives; history beyond that is marked
missing, never invented.
"""
from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SAFE_GIT = {"rev-parse", "diff", "status", "log", "show", "ls-files"}
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(repo_root: str, *args: str) -> tuple[int, str]:
    if not args or args[0] not in SAFE_GIT:
        raise ValueError(f"refusing non-read-only git command: {args[:1]}")
    try:
        # git emits UTF-8; the Windows locale codepage (cp1254, ...) cannot
        # decode it and a single Turkish character used to kill the diff
        # stdin closed: inside the MCP server stdin is the JSON-RPC pipe, and a
        # child that inherits it hung on Windows until the timeout
        r = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo_root, capture_output=True,
                           stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=60)
        return r.returncode, r.stdout or ""
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"git failed: {e}")


def is_repo(path: str) -> bool:
    try:
        code, out = _git(path, "rev-parse", "--is-inside-work-tree")
        return code == 0 and "true" in out
    except ValueError:
        return False


def current_sha(path: str) -> str:
    try:
        code, out = _git(path, "rev-parse", "HEAD")
        return out.strip() if code == 0 else ""
    except ValueError:
        return ""


def check_revision(repo_root: str, rev: str) -> None:
    if not rev or not re.fullmatch(r"[0-9a-fA-F]{4,40}|\w[\w./-]*", rev):
        raise ValueError(f"invalid revision: {rev!r}")
    code, _ = _git(repo_root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if code != 0:
        raise ValueError(f"unknown revision: {rev!r}")


@dataclass
class ChangedFile:
    rel: str
    status: str = "modified"  # added|modified|deleted|renamed|copied|typechange
    old_rel: str = ""
    new_ranges: list[tuple[int, int]] = field(default_factory=list)  # inclusive
    old_ranges: list[tuple[int, int]] = field(default_factory=list)


def _parse_name_status(out: str) -> list[ChangedFile]:
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0].strip()
        if code.startswith("R") and len(parts) >= 3:
            files.append(ChangedFile(parts[2].strip(), "renamed", parts[1].strip()))
        elif code.startswith("C") and len(parts) >= 3:
            files.append(ChangedFile(parts[2].strip(), "copied", parts[1].strip()))
        elif code.startswith("D"):
            files.append(ChangedFile(parts[-1].strip(), "deleted"))
        elif code.startswith("A"):
            files.append(ChangedFile(parts[-1].strip(), "added"))
        elif code.startswith("M"):
            files.append(ChangedFile(parts[-1].strip(), "modified"))
        elif code.startswith("T"):
            files.append(ChangedFile(parts[-1].strip(), "typechange"))
        elif code.startswith("U"):
            files.append(ChangedFile(parts[-1].strip(), "modified"))
    return files


def _hunk_ranges(out: str) -> dict[str, dict[str, list]]:
    cur = ""
    ranges: dict[str, dict[str, list]] = {}
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:].strip()
            ranges.setdefault(cur, {"new": [], "old": []})
        elif line.startswith("--- "):
            continue
        else:
            m = _HUNK.match(line)
            if m and cur:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                if count > 0:
                    ranges[cur]["new"].append((start, start + count - 1))
    return ranges


def diff_files(repo_root: str, base: str, head: str = "",
               separator: str = "...") -> list[ChangedFile]:
    """git diff between revisions (empty head = working tree target).

    `separator` preserves the caller's ``..`` vs ``...`` spelling so triple-dot
    (merge-base) and double-dot (direct range) keep their git semantics.
    """
    check_revision(repo_root, base)
    if head:
        check_revision(repo_root, head)
    spec = f"{base}{separator}{head}" if head else base
    _, names = _git(repo_root, "diff", "--name-status", "-M", spec)
    files = _parse_name_status(names)
    _, hunks = _git(repo_root, "diff", "-U0", "--no-color", "-M", spec)
    ranges = _hunk_ranges(hunks)
    for f in files:
        if f.rel in ranges:
            f.new_ranges = ranges[f.rel]["new"]
    return files


def working_tree_files(repo_root: str) -> list[ChangedFile]:
    """Uncommitted changes (staged + unstaged) vs HEAD, with hunk ranges."""
    if not is_repo(repo_root):
        raise ValueError(f"not a git directory: {repo_root}")
    _, names = _git(repo_root, "diff", "HEAD", "--name-status", "-M")
    if not names.strip():
        return []
    files = _parse_name_status(names)
    _, hunks = _git(repo_root, "diff", "HEAD", "-U0", "--no-color", "-M")
    ranges = _hunk_ranges(hunks)
    for f in files:
        if f.rel in ranges:
            f.new_ranges = ranges[f.rel]["new"]
    return files


def resolve_repo(repo_roots: list[str], rel_or_abs: str) -> tuple[str, str]:
    """Map a user-supplied path to (repo_id, repo-relative path)."""
    import hashlib
    p = Path(rel_or_abs)
    ap = (p if p.is_absolute() else (Path.cwd() / p)).resolve()
    for root in repo_roots:
        rr = Path(root).resolve()
        try:
            rel = str(ap.relative_to(rr)).replace("\\", "/")
            rid = "repo-" + hashlib.sha256(str(rr).encode()).hexdigest()[:10]
            return rid, rel
        except ValueError:
            continue
    raise ValueError(f"path escapes configured workspace roots: {rel_or_abs}")


def map_to_symbols(store, repo_id: str, changed: list[ChangedFile]) -> dict:
    """Map changed ranges to indexed symbols.

    Returns {"symbols": {node_id: {"change": added|modified|removed|...,
    "via": "range"|"file"}}, "unmapped_files": [...], "deleted_files": [...]}.
    Ranges that hit no symbol keep file-level impact (never dropped). Deleted
    files are reported separately for dangling-reference analysis.
    """
    symbols: dict[str, dict] = {}
    unmapped: list[str] = []
    deleted: list[str] = []
    for cf in changed:
        if cf.status == "deleted":
            deleted.append(cf.rel)
            continue
        rows = store.cx.execute(
            "SELECT * FROM nodes WHERE repository_id=? AND file_path=? AND type IN "
            "('function','method','class','interface','type','enum','test')",
            (repo_id, cf.rel)).fetchall()
        if not rows:
            unmapped.append(cf.rel)
            continue
        if not cf.new_ranges:
            for r in rows:
                from ..storage.sqlite_store import _row_node
                try:
                    n = _row_node(r)
                except Exception:
                    continue
                symbols.setdefault(n.id, {"change": "modified", "via": "file",
                                         "file": cf.rel, "status": cf.status})
            continue
        from ..storage.sqlite_store import _row_node
        hit_any = False
        for r in rows:
            try:
                n = _row_node(r)
            except Exception:
                continue
            loc = n.location
            s0 = getattr(loc, "start_line", 1) or 1
            s1 = getattr(loc, "end_line", None) or s0
            for a, b in cf.new_ranges:
                if a <= s1 and s0 <= b:
                    symbols.setdefault(n.id, {"change": "modified", "via": "range",
                                             "file": cf.rel, "status": cf.status})
                    hit_any = True
                    break
        if not hit_any:
            unmapped.append(cf.rel)
    return {"symbols": symbols, "unmapped_files": unmapped, "deleted_files": deleted}
