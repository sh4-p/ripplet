"""Compact file views for large reads.

An agent's whole-file `Read` of a big source file is the largest single item
in its context, and it is re-sent on every later turn. When the index knows
the file, a view keeps the parts that matter in full (symbols the task names,
grep hits, their graph neighbours) and collapses the rest to one signature
line per symbol, with the real line ranges so the agent can read any part
back with `offset`/`limit`.

Never shrinks: small files, reads that already chose a range, files the index
does not have or whose content changed since indexing, non-code files.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

MIN_LINES = 400           # files at or below this are always shown whole
BUDGET_FRACTION = 0.25    # shown code lines: at most this share of the file...
BUDGET_MIN, BUDGET_MAX = 80, 300   # ...clamped to this many lines
HEAD_LINES = 25           # file prologue (imports, module docstring) kept
CODE_EXT = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".java", ".kt", ".kts",
            ".rs", ".cs", ".php", ".swift", ".dart", ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx",
            ".lua", ".gd", ".rb", ".scala"}
_SYM_TYPES = ("function", "method", "class", "interface", "struct", "type", "trait", "enum",
              "object", "test")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


@dataclass
class Symbol:
    id: str
    label: str
    kind: str
    start: int
    end: int
    callers: int = 0
    score: float = 0.0
    children: list = field(default_factory=list)


@dataclass
class View:
    content: str
    shown_lines: int
    total_lines: int
    sections: list           # [(start, end, label)] shown in full
    reason: str = ""


def find_index(path: Path):
    """(db path, repo id, repo root, rel path) for an indexed file, or None."""
    p = path.resolve()
    for d in [p.parent, *p.parents]:
        from ..legacy import config_path
        cfg = config_path(d)
        if not cfg.is_file():
            continue
        try:
            c = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        db = Path(c.get("storage_path") or d / ".ripplet" / "graph.db")
        if not db.is_absolute():
            db = d / db
        for root in c.get("workspace_roots") or [str(d)]:
            r = Path(root).resolve()
            try:
                rel = p.relative_to(r).as_posix()
            except ValueError:
                continue
            rid = "repo-" + hashlib.sha256(str(r).encode()).hexdigest()[:10]
            return db, rid, r, rel
        return None
    return None


def _fresh(cx, rid: str, rel: str, raw: bytes) -> bool:
    row = cx.execute("SELECT content_hash FROM nodes WHERE type='file' AND repository_id=? "
                     "AND file_path=?", (rid, rel)).fetchone()
    if not row or not row[0]:
        return False
    text = raw.decode("utf-8", "ignore")
    cands = {"sha256:" + hashlib.sha256(raw).hexdigest(),
             "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
             "sha256:" + hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()}
    return row[0] in cands


def outline(cx, rid: str, rel: str) -> list[Symbol]:
    syms: list[Symbol] = []
    rows = cx.execute(
        f"SELECT id, label, type, start_line, end_line FROM nodes WHERE repository_id=? AND file_path=? "
        f"AND type IN ({','.join('?' * len(_SYM_TYPES))}) AND start_line > 0", (rid, rel, *_SYM_TYPES)).fetchall()
    for nid, label, typ, s, e in rows:
        syms.append(Symbol(nid, label or "", typ, int(s or 0), int(e or s or 0)))
    if syms:
        ids = [s.id for s in syms]
        counts: dict = {}
        for i in range(0, len(ids), 500):
            ch = ids[i:i + 500]
            for tgt, n in cx.execute(
                    f"SELECT target, COUNT(*) FROM edges WHERE relation IN ('calls','instantiates','calls_http') "
                    f"AND target IN ({','.join('?' * len(ch))}) GROUP BY target", ch):
                counts[tgt] = n
        for s in syms:
            s.callers = counts.get(s.id, 0)
    syms.sort(key=lambda s: (s.start, -(s.end - s.start)))
    return syms


def _neighbours(cx, ids: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(ids), 400):
        ch = ids[i:i + 400]
        q = ",".join("?" * len(ch))
        for a, b in cx.execute(f"SELECT source, target FROM edges WHERE relation IN ('calls','instantiates') "
                               f"AND (source IN ({q}) OR target IN ({q}))", ch + ch):
            out.add(a)
            out.add(b)
    return out


def score(cx, syms: list[Symbol], terms: list[str], hit_lines: set[int]) -> None:
    """Task relevance: named in the task (strong), contains a grep hit or a
    term in its name (medium), graph neighbour of a strong symbol (weak)."""
    tl = {t.lower() for t in terms if t}
    strong = []
    for s in syms:
        lab = s.label.lower()
        if lab and lab in tl:
            s.score += 10
            strong.append(s.id)
        elif lab and any(len(t) >= 4 and (t in lab or lab in t) for t in tl):
            s.score += 3
        if any(s.start <= ln <= s.end for ln in hit_lines):
            s.score += 6
    if strong:
        near = _neighbours(cx, strong)
        for s in syms:
            if s.id in near and s.id not in strong:
                s.score += 2


def _leaf_sections(syms: list[Symbol]) -> list[Symbol]:
    """Innermost symbols (methods rather than their whole class) so one hit
    does not drag a 2,000-line class into the view."""
    out = []
    for s in syms:
        inner = [t for t in syms if t is not s and s.start <= t.start and t.end <= s.end
                 and (t.end - t.start) < (s.end - s.start)]
        if not inner or s.score > max(t.score for t in inner):
            out.append(s)
    return out


def render(lines: list[str], syms: list[Symbol], rel: str, budget: int) -> View:
    total = len(lines)
    picks: list[Symbol] = []
    used = 0
    for s in sorted(_leaf_sections(syms), key=lambda x: (-x.score, -x.callers, x.start)):
        if s.score <= 0:
            break
        size = s.end - s.start + 1
        if used + size > budget and picks:
            continue
        if any(not (s.end < p.start or s.start > p.end) for p in picks):
            continue
        picks.append(s)
        used += size
    head_end = min(HEAD_LINES, (min((s.start for s in syms), default=total + 1) - 1) or HEAD_LINES)
    ranges = [(1, max(1, min(head_end, total)), "prologue")] + \
        sorted(((p.start, min(p.end, total), p.label) for p in picks), key=lambda r: r[0])
    shown = set()
    for a, b, _l in ranges:
        shown.update(range(a, b + 1))
    out = [f"[ripplet view] {rel}: {total} lines; showing {len(shown)} relevant lines in full and an "
           f"outline of the rest. Sections are marked @@ Lstart-end @@ with real file line numbers; "
           f"the editor's own numbering in this view is NOT the file's. To see any other part, "
           f"Read this file again with offset=<line> limit=<n> (a second plain Read returns the whole file).",
           ""]
    for a, b, lab in ranges:
        out.append(f"@@ L{a}-{b} @@ {lab}")
        out.extend(lines[a - 1:b])
        out.append("")
    collapsed = [s for s in syms if not any(s.start >= a and s.end <= b for a, b, _ in ranges)]
    if collapsed:
        out.append("@@ outline of the parts not shown @@")
        for s in collapsed:
            sig = lines[s.start - 1].strip() if 0 < s.start <= total else s.label
            calls = f"  [{s.callers} callers]" if s.callers else ""
            out.append(f"L{s.start}-{s.end}  {sig[:160]}{calls}")
    return View("\n".join(out), len(shown), total, [(a, b, l) for a, b, l in ranges])


def build_view(path: str, terms: list[str] | None = None, hit_lines: set[int] | None = None,
               offset=None, limit=None, force: bool = False) -> View | None:
    """A compact view of `path`, or None when the whole file should be shown."""
    p = Path(path)
    if offset or limit:
        return None
    if p.suffix.lower() not in CODE_EXT or not p.is_file():
        return None
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    lines = raw.decode("utf-8", "ignore").splitlines()
    if len(lines) <= MIN_LINES and not force:
        return None
    loc = find_index(p)
    if loc is None:
        return None
    db, rid, _root, rel = loc
    if not Path(db).is_file():
        return None
    cx = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        if not _fresh(cx, rid, rel, raw):
            return None
        syms = outline(cx, rid, rel)
        if len(syms) < 2:
            return None
        score(cx, syms, list(terms or []), set(hit_lines or ()))
    finally:
        cx.close()
    budget = max(BUDGET_MIN, min(BUDGET_MAX, int(len(lines) * BUDGET_FRACTION)))
    v = render(lines, syms, rel, budget)
    if v.shown_lines >= 0.8 * len(lines):
        return None   # barely smaller: not worth the indirection
    return v


def task_terms(prompt_text: str) -> list[str]:
    """Identifier-like words of the task (a symbol named in the prompt)."""
    return sorted({w for w in _IDENT.findall(prompt_text or "")})
