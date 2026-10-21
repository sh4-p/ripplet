from __future__ import annotations
import fnmatch
import re
from functools import lru_cache
from pathlib import Path
DEFAULT_DIRS = {".git","__pycache__",".venv","venv","node_modules","vendor","bower_components",".mypy_cache",".pytest_cache","dist","build",".ripplet",".atlasgraph","target",".idea",".vscode",
                # build output and other tools' caches: never source
                "bin","obj","TestResults",".vs",".gradle",".next",".nuxt",".angular",".dart_tool",
                "Pods","DerivedData",".terraform",".tox",".ruff_cache",".cache","coverage","htmlcov",
                # native/mobile build trees (CMake, Android NDK, Xcode, Flutter, Expo)
                ".cxx","CMakeFiles",".externalNativeBuild","xcuserdata",".expo",".svelte-kit",".turbo",".parcel-cache",
                # coding-agent configuration (hooks, plugins, MCP bindings): not project code
                ".claude",".opencode",".cursor",".codex"}
DEFAULT_PATTERNS = ["*.pyc","*.pyo","*.egg-info*","*.min.js","*.map",".mcp.json","opencode.json"]
def load_gitignore(root: str) -> list[str]:
    p = Path(root)/".gitignore"
    if not p.exists(): return []
    out=[]
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line=line.rstrip()
        if line.strip() and not line.lstrip().startswith("#"): out.append(line.strip())
    return out


def _glob_to_regex(pat: str) -> str:
    """gitignore glob -> regex over a '/'-separated path. `*` and `?` never
    cross '/'; `**/` matches zero or more directories; `/**` everything below."""
    i, out = 0, []
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pat.startswith("/**", i) and i + 3 == len(pat):
            out.append("/.*"); i += 3
        elif pat.startswith("**", i):
            out.append(".*"); i += 2
        elif pat[i] == "*":
            out.append("[^/]*"); i += 1
        elif pat[i] == "?":
            out.append("[^/]"); i += 1
        elif pat[i] == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out.append(re.escape(pat[i])); i += 1
            else:
                cls = pat[i + 1:j].replace("\\", "\\\\")
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append(f"[{cls}]"); i = j + 1
        else:
            out.append(re.escape(pat[i])); i += 1
    return "".join(out)


@lru_cache(maxsize=64)
def _compile(patterns: tuple[str, ...]) -> tuple:
    """(negate, dir_only, regex, anchored) per rule, in file order.

    Git semantics: a pattern without a '/' (other than a trailing one)
    matches a name at any depth; with a '/' it is relative to the root;
    a trailing '/' matches directories only; `!` re-includes; the last
    matching rule wins. The old matcher used `rel.startswith(pat)`, so
    `test` (meant for a build binary) hid `tests/` and `test.c`."""
    rules = []
    for raw in patterns:
        pat = raw.strip()
        if not pat or pat.startswith("#"):
            continue
        neg = pat.startswith("!")
        if neg:
            pat = pat[1:]
        if pat.startswith("\\"):
            pat = pat[1:]
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        if not pat:
            continue
        anchored = "/" in pat
        pat = pat.lstrip("/")
        rules.append((neg, dir_only, re.compile(_glob_to_regex(pat) + r"\Z"), anchored))
    return tuple(rules)


def _gitignored(rel: str, is_dir: bool, rules: tuple) -> bool:
    name = rel.rsplit("/", 1)[-1]
    ignored = False
    for neg, dir_only, rx, anchored in rules:
        if dir_only and not is_dir:
            continue
        hit = bool(rx.match(rel)) if anchored else bool(rx.match(name))
        if hit:
            ignored = not neg
    return ignored


def should_ignore(rel: str, is_dir: bool, extra: list[str] | None = None) -> bool:
    rel = rel.replace("\\","/")
    parts = rel.split("/")
    for d in parts:
        if d in DEFAULT_DIRS: return True
    name = parts[-1]
    for pat in DEFAULT_PATTERNS:
        if fnmatch.fnmatch(name, pat): return True
    if extra:
        # Directories are pruned during the walk, so a file only needs its
        # own path checked (parents were already evaluated as directories).
        return _gitignored(rel, is_dir, _compile(tuple(extra)))
    return False
