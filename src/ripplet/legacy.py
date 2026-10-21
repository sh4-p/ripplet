"""Compatibility with indexes and settings written under the previous name.

Ripplet was called AtlasGraph. Projects indexed before the rename have a
`.atlasgraph/` directory, may set `ATLASGRAPH_*` environment variables, and
may carry agent hooks, rules blocks and MCP bindings under the old name.
Everything here keeps those working and moves them over when it safely can.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

INDEX_DIR = ".ripplet"
LEGACY_INDEX_DIR = ".atlasgraph"
LEGACY_NAME = "atlasgraph"


def adopt_legacy_env() -> None:
    """`ATLASGRAPH_X` -> `RIPPLET_X` when the new one is not set."""
    for k, v in list(os.environ.items()):
        if k.startswith("ATLASGRAPH_"):
            os.environ.setdefault("RIPPLET_" + k[len("ATLASGRAPH_"):], v)


def index_dir(project: Path) -> Path:
    """The project's index directory: `.ripplet`, or a not yet migrated
    `.atlasgraph` when only that one exists."""
    new = Path(project) / INDEX_DIR
    old = Path(project) / LEGACY_INDEX_DIR
    if not new.exists() and (old / "config.json").is_file():
        return old
    return new


def config_path(project: Path) -> Path:
    return index_dir(project) / "config.json"


def migrate(project: Path) -> bool:
    """Rename `<project>/.atlasgraph` to `.ripplet` and point its config at
    the new location. Returns False (and leaves everything usable under the
    old name) when the directory is in use or cannot be moved."""
    project = Path(project)
    old, new = project / LEGACY_INDEX_DIR, project / INDEX_DIR
    if new.exists() or not (old / "config.json").is_file():
        return False
    try:
        old.rename(new)
    except OSError:
        return False                 # a process holds the database open: retry next time
    cp = new / "config.json"
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        sp = str(data.get("storage_path") or "")
        for sep in ("/", "\\"):
            sp = sp.replace(f"{sep}{LEGACY_INDEX_DIR}{sep}", f"{sep}{INDEX_DIR}{sep}")
        if sp.startswith(LEGACY_INDEX_DIR + "/") or sp.startswith(LEGACY_INDEX_DIR + "\\"):
            sp = INDEX_DIR + sp[len(LEGACY_INDEX_DIR):]
        data["storage_path"] = sp
        cp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass
    return True


def migrate_upwards(start: Path) -> None:
    """Migrate the nearest project at or above `start` (best effort)."""
    try:
        p = Path(start).resolve()
    except OSError:
        return
    for d in [p, *p.parents]:
        if (d / INDEX_DIR).exists():
            return
        if (d / LEGACY_INDEX_DIR / "config.json").is_file():
            migrate(d)
            return


def find_config(start: Path) -> Path | None:
    """Nearest `<dir>/.ripplet/config.json` (or legacy) at or above `start`."""
    p = Path(start).resolve()
    for d in [p, *p.parents]:
        c = config_path(d)
        if c.is_file():
            return c
    return None
