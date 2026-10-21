from __future__ import annotations
import hashlib, os
from dataclasses import dataclass
from pathlib import Path
from .ignore_rules import should_ignore, load_gitignore
@dataclass
class FileRecord:
    abs_path: str
    rel_path: str
    size: int
    sha256: str
    repo_id: str = "default"
def fingerprint(path: str) -> str:
    h = hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda: f.read(65536), b""): h.update(chunk)
    return "sha256:"+h.hexdigest()
def _minified(path: str, size: int) -> bool:
    """A bundled/minified asset (one huge line, no structure): third-party
    build output whose symbols are noise (`<callback#13>.h`), not source."""
    if size < 4000:
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return False
    lines = head.split(b"\n")
    longest = max((len(x) for x in lines), default=0)
    return longest > 1500 and len(head) / max(1, len(lines)) > 300


def scan_workspace(root: str, repo_id: str = "default", max_bytes: int = 2_000_000, include_exts: set[str] | None = None) -> list[FileRecord]:
    rootp = Path(root).resolve()
    extra = load_gitignore(str(rootp))
    out: list[FileRecord] = []
    for dirpath, dirnames, filenames in os.walk(rootp):
        rel_dir = os.path.relpath(dirpath, rootp).replace("\\","/")
        if rel_dir == ".": rel_dir = ""
        dirnames[:] = [d for d in dirnames if not should_ignore(((rel_dir+"/"+d) if rel_dir else d), True, extra)]
        for fn in filenames:
            rel = ((rel_dir+"/"+fn) if rel_dir else fn)
            if should_ignore(rel, False, extra): continue
            ap = str(Path(dirpath)/fn)
            try: sz = os.path.getsize(ap)
            except OSError: continue
            if sz > max_bytes: continue
            # a big JSON file is a data dump, and JSON under a cache folder is a tool's
            # cache: neither is configuration
            if fn.lower().endswith(".json") and (sz > 256_000 or "cache" in rel.lower().split("/")[:-1]): continue
            if fn.lower().endswith((".js", ".mjs", ".cjs", ".css")) and _minified(ap, sz): continue
            if include_exts is not None:
                ext = Path(fn).suffix.lower()
                if ext not in include_exts: continue
            try: fp = fingerprint(ap)
            except OSError: continue
            out.append(FileRecord(ap, rel.replace("\\","/"), sz, fp, repo_id))
    out.sort(key=lambda r: r.rel_path)
    return out
