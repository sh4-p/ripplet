"""Compact grep results: hits grouped under the symbol that contains them.

Content-mode grep over a repo returns one line per hit; on a common name
that is hundreds of lines, most in vendored or generated files, all carried
in the agent's context from then on. Shaped output keeps every hit's file and
real line number but groups hits by enclosing function (from the index),
collapses long runs inside one symbol and vendored/generated files to counts,
and says how to get the rest. Untouched: file-list and count modes, context
(-A/-B/-C) output, small results, unparseable lines.
"""
from __future__ import annotations

import re
import sqlite3
from collections import OrderedDict
from pathlib import Path

from .fileview import find_index, outline

MIN_HIT_LINES = 40        # results at or below this are left as they are
PER_SYMBOL = 4            # hits shown per enclosing symbol before collapsing
_VENDOR = re.compile(r"(^|/)(node_modules|vendor|third_party|dist|build|\.venv|site-packages)/|"
                     r"\.min\.(js|css)$|(\.generated\.|_pb2\.py$|\.g\.dart$|\.pb\.go$)")
_MULTI = re.compile(r"^(.*?):(\d+):(.*)$")
_SINGLE = re.compile(r"^(\d+):(.*)$")


def _parse(content: str, single_path: str | None):
    hits = []
    for ln in content.splitlines():
        m = _SINGLE.match(ln) if single_path else _MULTI.match(ln)
        if not m:
            return None   # unknown shape: never guess
        if single_path:
            hits.append((single_path, int(m.group(1)), m.group(2)))
        else:
            hits.append((m.group(1), int(m.group(2)), m.group(3)))
    return hits


def shape_grep(tool_input: dict, content: str, cwd: str) -> tuple[str, dict] | None:
    """(new content, {file: [hit lines]}) or None to keep the original."""
    if (tool_input.get("output_mode") or "files_with_matches") != "content":
        return None
    if any(tool_input.get(k) for k in ("-A", "-B", "-C", "context")):
        return None
    if content.count("\n") + 1 <= MIN_HIT_LINES:
        return None
    path = tool_input.get("path") or ""
    single = path if path and Path(path if Path(path).is_absolute() else Path(cwd) / path).is_file() else None
    hits = _parse(content, single)
    if not hits:
        return None
    by_file: "OrderedDict[str, list]" = OrderedDict()
    for f, ln, text in hits:
        by_file.setdefault(f, []).append((ln, text))
    out, hit_lines, vendored = [], {}, []
    shown = collapsed = 0
    cx_cache: dict = {}
    for f, items in by_file.items():
        fp = Path(f) if Path(f).is_absolute() else Path(cwd) / f
        rel_disp = f.replace("\\", "/")
        if _VENDOR.search(rel_disp):
            vendored.append((rel_disp, len(items)))
            continue
        hit_lines[str(fp)] = [ln for ln, _t in items]
        syms = []
        loc = find_index(fp) if fp.is_file() else None
        if loc and Path(loc[0]).is_file():
            key = str(loc[0])
            if key not in cx_cache:
                cx_cache[key] = sqlite3.connect(f"file:{loc[0]}?mode=ro", uri=True)
            syms = [s for s in outline(cx_cache[key], loc[1], loc[3]) if s.kind in ("function", "method", "test")]
        groups: "OrderedDict[str, list]" = OrderedDict()
        for ln, text in items:
            enc = [s for s in syms if s.start <= ln <= s.end]
            name = (min(enc, key=lambda s: s.end - s.start).label + f" (L{min(enc, key=lambda s: s.end - s.start).start})"
                    if enc else "(module level)")
            groups.setdefault(name, []).append((ln, text))
        out.append(f"{rel_disp}  [{len(items)} hits]")
        for name, gl in groups.items():
            out.append(f"  > {name}")
            for ln, text in gl[:PER_SYMBOL]:
                out.append(f"    {ln}: {text.strip()[:220]}")
                shown += 1
            if len(gl) > PER_SYMBOL:
                out.append(f"    … {len(gl) - PER_SYMBOL} more hits in this symbol (lines "
                           f"{', '.join(str(x[0]) for x in gl[PER_SYMBOL:PER_SYMBOL + 12])}"
                           f"{', …' if len(gl) > PER_SYMBOL + 12 else ''})")
                collapsed += len(gl) - PER_SYMBOL
    for c in cx_cache.values():
        c.close()
    head = (f"[ripplet grep] {len(hits)} hits in {len(by_file)} files, grouped by enclosing symbol; "
            f"line numbers are real. {collapsed} repeated hits collapsed"
            + (f", {sum(n for _f, n in vendored)} hits in {len(vendored)} vendored/generated files counted only"
               if vendored else "")
            + ". For every hit, grep again with a narrower path or glob.")
    if vendored:
        out.append("vendored/generated (not listed): " + ", ".join(f"{f} [{n}]" for f, n in vendored[:15]))
    new = head + "\n" + "\n".join(out)
    if len(new) >= 0.85 * len(content):
        return None   # no real gain: keep grep's own output
    return new, hit_lines
