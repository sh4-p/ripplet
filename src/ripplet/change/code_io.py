"""Read and edit code through Ripplet: the experimental "sandbox" tools (read_code, edit_code, write_code).

An agent that opens a whole file only to be allowed to edit it pays for every line, and pays again on each
later turn. These helpers let it read a symbol or a line range, change code by exact replacement (several
edits at once, all or nothing) and create files without leaving Ripplet.

Guarantees: every path stays inside the workspace and out of tool folders (.git, .ripplet, node_modules...);
each file keeps its own line endings; an edit applies only when `old` matches exactly one place (or
replace_all is set); when any edit of a call fails nothing is written.
"""
from __future__ import annotations

import difflib
from pathlib import Path

MAX_READ_LINES = 400
MAX_FILE_BYTES = 2_000_000
_BLOCKED = {".git", ".ripplet", "node_modules", ".venv", "venv", "__pycache__", ".claude", ".opencode",
            ".idea", ".vscode"}


def _roots(cfg) -> list:
    return [Path(r).resolve() for r in (getattr(cfg, "workspace_roots", None) or ["."])]


def locate(cfg, path: str, must_exist: bool = True):
    """(absolute path, workspace-relative posix path) of a file inside the workspace, else (None, reason)."""
    raw = Path(str(path).strip().strip("\"'"))
    for root in _roots(cfg):
        p = raw if raw.is_absolute() else root / raw
        try:
            p = p.resolve()
            rel = p.relative_to(root)
        except (ValueError, OSError):
            continue
        blocked = sorted(_BLOCKED & set(rel.parts))
        if blocked:
            return None, f"{path}: {blocked[0]}/ is off limits"
        if p.exists() or not must_exist:
            return p, rel.as_posix()
    return None, (f"{path}: no such file in the workspace" if must_exist else f"{path}: outside the workspace")


def _read_text(p: Path):
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None, "larger than 2 MB"
        data = p.read_bytes()
    except OSError as e:
        return None, str(e)
    if b"\0" in data[:8192]:
        return None, "binary file"
    return data.decode("utf-8", errors="replace"), ""


def _eol(text: str) -> str:
    """The file's own line ending: CRLF when most of its lines end that way."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf > text.count("\n") - crlf else "\n"


def _numbered(lines: list, start: int) -> list:
    return [f"{start + i:>5}| {t}" for i, t in enumerate(lines)]


def read_lines(cfg, path: str, start=None, end=None, header: str | None = None) -> str:
    """Numbered lines of a workspace file (at most MAX_READ_LINES per call)."""
    p, rel = locate(cfg, path)
    if p is None:
        return f"error: {rel}"
    if p.is_dir():
        names = sorted(c.name + ("/" if c.is_dir() else "") for c in p.iterdir() if c.name not in _BLOCKED)
        return f"== {rel}/ is a folder: " + ", ".join(names[:80]) + (" ..." if len(names) > 80 else "")
    text, why = _read_text(p)
    if text is None:
        return f"error: {rel}: {why}"
    lines = text.splitlines()
    total = len(lines)
    s = max(1, int(start or 1))
    e = min(total, int(end) if end else s + MAX_READ_LINES - 1, s + MAX_READ_LINES - 1)
    out = [f"{header or '== ' + rel}  (lines {s}-{e} of {total})"] + _numbered(lines[s - 1:e], s)
    if e < total:
        out.append(f'  [lines {e + 1}-{total} not shown: read_code(targets=["{rel}"], start={e + 1})]')
    return "\n".join(out)


def _window(body: str, start, end):
    """Character range of lines start..end (1-based) of body."""
    offs = [0]
    for i, ch in enumerate(body):
        if ch == "\n":
            offs.append(i + 1)
    s = max(1, int(start or 1))
    lo = offs[min(s - 1, len(offs) - 1)]
    hi = offs[min(int(end), len(offs) - 1)] if end and int(end) < len(offs) else len(body)
    return lo, max(hi, lo)


def _hint(seg: str, old: str) -> str:
    """Why `old` was not found: the closest line of the file, so a whitespace slip is obvious."""
    first = next((ln.strip() for ln in old.split("\n") if ln.strip()), "")
    if not first:
        return ""
    lines = seg.split("\n")
    near = difflib.get_close_matches(first, [ln.strip() for ln in lines], n=1, cutoff=0.6)
    if not near:
        return "; no similar line in that file (read_code it first)"
    at = next(i for i, ln in enumerate(lines, 1) if ln.strip() == near[0])
    return f"; closest line is {at}: {lines[at - 1].strip()[:100]!r} (indentation or wording differs)"


def apply_edits(cfg, edits: list) -> str:
    """Apply [{path, old, new, replace_all?, start?, end?}] atomically; returns a short report."""
    work: dict = {}          # path -> [text with \n, eol, rel, first edit done]
    reports, errors = [], []
    for n, ed in enumerate(edits or [], 1):
        path, old, new = ed.get("path"), ed.get("old"), ed.get("new")
        if not path or old is None or new is None:
            errors.append(f"edit {n}: needs path (or symbol), old and new")
            continue
        if old == new:
            errors.append(f"edit {n}: old and new are the same")
            continue
        if old == "":
            errors.append(f"edit {n}: old is empty (use write_code to create a file)")
            continue
        p, rel = locate(cfg, path)
        if p is None:
            errors.append(f"edit {n}: {rel}")
            continue
        if p not in work:
            text, why = _read_text(p)
            if text is None:
                errors.append(f"edit {n}: {rel}: {why}")
                continue
            work[p] = [text.replace("\r\n", "\n"), _eol(text), rel, False]
        body = work[p][0]
        old_n, new_n = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        # the line span of a symbol narrows the match, for the first edit of a file only (later edits move lines)
        lo, hi = (_window(body, ed.get("start"), ed.get("end")) if (ed.get("start") or ed.get("end"))
                  and not work[p][3] else (0, len(body)))
        seg = body[lo:hi]
        count = seg.count(old_n)
        if count == 0:
            errors.append(f"edit {n}: {rel}: `old` not found{_hint(seg, old_n)}")
            continue
        if count > 1 and not ed.get("replace_all"):
            errors.append(f"edit {n}: {rel}: `old` matches {count} places; include more surrounding lines "
                          f"so it is unique, or set replace_all")
            continue
        line_no = body[:lo + seg.find(old_n)].count("\n") + 1
        seg2 = seg.replace(old_n, new_n) if ed.get("replace_all") else seg.replace(old_n, new_n, 1)
        work[p][0] = body[:lo] + seg2 + body[hi:]
        work[p][3] = True
        after = work[p][0].split("\n")
        a, b = max(1, line_no - 2), min(len(after), line_no + new_n.count("\n") + 2)
        reports.append((rel, n, line_no, old_n.count("\n") + 1, new_n.count("\n") + 1, _numbered(after[a - 1:b][:16], a)))
    if errors:
        return "nothing written:\n- " + "\n- ".join(errors)
    for p, (text, eol, rel, _done) in work.items():
        try:
            p.write_bytes(text.replace("\n", eol).encode("utf-8"))
        except OSError as e:
            return f"error: could not write {rel}: {e} (earlier files of this call may already be written)"
    out = [f"applied {len(reports)} edit(s) in {len(work)} file(s):"]
    for rel, n, line_no, was, now, excerpt in reports:
        out.append(f"{rel}: edit {n} at line {line_no} ({was} line(s) -> {now})")
        out.extend(excerpt)
    out.append("The index refreshes on the next Ripplet call; check_change shows what the edit broke.")
    return "\n".join(out)


def write_file(cfg, path: str, content: str, overwrite: bool = False) -> str:
    """Create a file (parent folders included); an existing file is replaced only with overwrite=true."""
    p, rel = locate(cfg, path, must_exist=False)
    if p is None:
        return f"error: {rel}"
    exists = p.exists()
    if exists and not overwrite:
        return f"error: {rel} exists; use edit_code, or pass overwrite=true to replace it"
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return f"error: {rel}: content is larger than 2 MB"
    eol = "\n"
    if exists:
        old, _ = _read_text(p)
        eol = _eol(old) if old else "\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content.replace("\r\n", "\n").replace("\n", eol).encode("utf-8"))
    return f"{'replaced' if exists else 'created'} {rel} ({content.count(chr(10)) + 1} lines)"
