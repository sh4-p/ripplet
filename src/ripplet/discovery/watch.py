"""Filesystem event source for watch mode (Phase 7 Stages 1-2).

watchfiles is OPTIONAL: base installs work without it and `ripplet watch`
fails with an actionable message. Events are hints only — the reconciliation
scan in `service.index_workspace` is the source of truth, so lost, duplicated
or reordered notifications can never corrupt the graph by themselves.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

WATCH_EXTRA = "watch"
WATCH_HINT = "pip install 'ripplet[watch]'"


def available() -> bool:
    try:
        import watchfiles  # noqa: F401
        return True
    except ImportError:
        return False


def require_available() -> None:
    if not available():
        raise RuntimeError(
            "filesystem monitoring needs the optional watch backend "
            f"({WATCH_HINT}); manual `ripplet index` keeps working.")


@dataclass(frozen=True)
class FsEvent:
    kind: str  # added | modified | deleted
    abs_path: str
    repo_root: str = ""

    def rel(self) -> str:
        try:
            return str(Path(self.abs_path).relative_to(self.repo_root)).replace("\\", "/")
        except Exception:
            return self.abs_path.replace("\\", "/")


# Never treat our own outputs or volatile junk as source changes.
ALWAYS_IGNORE_DIRS = frozenset({
    ".ripplet", ".atlasgraph", ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", "target", ".idea", ".vscode",
})
ALWAYS_IGNORE_SUFFIXES = (".lock", ".tmp", ".swp", ".swo", "~", ".pyc", ".pyo")
ATOMIC_TMP_PREFIXES = (".goutputstream-", ".~", "#", ".nfs")


def is_volatile(abs_path: str) -> bool:
    name = Path(abs_path).name
    if name.endswith(ALWAYS_IGNORE_SUFFIXES):
        return True
    if name.startswith(ATOMIC_TMP_PREFIXES) or name.endswith("~"):
        return True
    for part in Path(abs_path).parts:
        if part in ALWAYS_IGNORE_DIRS:
            return True
    return False


def coalesce_events(events: list[FsEvent]) -> dict[str, str]:
    """Merge a burst into {rel_path: net_kind}. Pure function (deterministic).

    Rules: added+deleted with nothing in between vanishes (the scan would find
    nothing either way); any deleted wins over prior adds/modifies; anything
    after a delete re-arms (re-created). Kinds collapse to added|modified|
    deleted — the scan, not the flag, decides the final disposition.
    """
    state: dict[str, str] = {}
    for ev in events:
        rel = ev.rel()
        if not rel:
            continue
        cur = state.get(rel)
        if ev.kind == "deleted":
            state[rel] = "deleted"
        elif cur == "deleted":
            state[rel] = "added" if ev.kind == "added" else "modified"
        elif cur is None:
            state[rel] = ev.kind if ev.kind in ("added", "modified", "deleted") else "modified"
        # else: keep the earliest non-deleted kind (added stays added)
    # added+deleted pairs with no survivor content vanish only when the file is
    # really gone; keep the tombstone so reconciliation confirms via scan.
    return state


def batch_is_config_only(coalesced: dict[str, str]) -> bool:
    names = {r.rsplit("/", 1)[-1].lower() for r in coalesced}
    return bool(names) and names <= {"tsconfig.json", "tsconfig.base.json", "package.json",
                                     "package-lock.json", "pyproject.toml", "setup.cfg",
                                     ".gitignore", ".gitattributes"}


@dataclass
class WatchFilter:
    """Maps watchfiles output onto repo-relative FsEvents, dropping our own
    database/lock files and volatile paths before they reach the queue."""

    roots: list[str] = field(default_factory=list)

    def normalize(self, changes: set) -> list[FsEvent]:
        out: list[FsEvent] = []
        for change, raw in changes:
            ap = str(raw)
            if is_volatile(ap):
                continue
            try:
                from watchfiles import Change as _C
                kind = { _C.added: "added", _C.modified: "modified",
                         _C.deleted: "deleted" }.get(change, "modified")
            except Exception:
                kind = "modified"
            root = ""
            for r in self.roots:
                try:
                    Path(ap).relative_to(r)
                    root = r
                    break
                except Exception:
                    continue
            if root or not self.roots:
                out.append(FsEvent(kind, ap, root))
        return out

    def watch_kwargs(self) -> dict:
        from watchfiles import DefaultFilter
        # NOTE: ignore_entity_patterns are REGEXES, not globs.
        filt = DefaultFilter(ignore_dirs=list(ALWAYS_IGNORE_DIRS),
                             ignore_entity_patterns=[r"\.lock$", r"\.tmp$", r"\.swp$",
                                                      r"\.swo$", r"\.pyc$", r"~$"])
        return {"watch_filter": filt, "debounce": 500, "recursive": True}
