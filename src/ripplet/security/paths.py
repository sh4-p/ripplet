from __future__ import annotations
import os
from pathlib import Path
from ..core.errors import SecurityError
def resolve_and_check(user_path: str, roots: list[str]) -> Path:
    p = Path(user_path)
    if not p.is_absolute():
        if not roots: raise SecurityError("relative path without workspace root")
        p = Path(roots[0]) / p
    resolved = p.resolve()
    for r in roots:
        rr = Path(r).resolve()
        try:
            resolved.relative_to(rr)
            return resolved
        except ValueError: continue
    raise SecurityError(f"path escapes workspace roots: {user_path}")
def is_within(path: str, root: str) -> bool:
    try: Path(path).resolve().relative_to(Path(root).resolve()); return True
    except ValueError: return False
def safe_join(root: str, *parts: str) -> Path:
    base = Path(root).resolve()
    p = (base.joinpath(*parts)).resolve()
    try: p.relative_to(base)
    except ValueError: raise SecurityError("path traversal blocked")
    return p
