"""Query-relevant evidence spans + progressive expansion (Phase 19).

Span-first presentation keeps every fact, evidence row, and qualification,
but replaces file-prefix evidence quotes with exact symbol-range spans
sliced from revision-verified source. Nothing is summarized or rewritten.

Revision safety: a span is served only when the on-disk file's sha256
matches the indexed files-table sha for that (repo, relpath). Anything
else (missing file, hash mismatch, undecodable bytes, degenerate ranges)
falls back to the indexed quote with explicit stale/unavailable marking —
never new-revision text presented as old evidence.

Expansion (`expand_evidence`) re-reads by canonical evidence/node identity
at symbol/context/file granularity, with the same verification and bounded
output. No server-side conversational memory: callers pass identities per
call (the Phase-17 ledger pattern works unchanged).
"""
from __future__ import annotations
import hashlib
from pathlib import Path

SYMBOL_SPAN_TYPES = {"function", "method", "class", "test", "constant",
                     "interface", "type", "enum"}
MAX_SPAN_LINES = 120
MAX_SPAN_BYTES = 8192
CONTEXT_LINES = 10
MAX_EXPAND_BYTES = 20000


def _roots(cfg) -> list[str]:
    return [str(Path(r).resolve()) for r in (getattr(cfg, "workspace_roots", None) or [])]


def resolve_source(cfg, repo_id: str, rel: str):
    """Resolve (repo, relpath) to an absolute file inside a workspace root.
    Returns (abs_path, repo_root) or (None, None) on traversal/escape."""
    if not rel or rel.startswith(("ext:", "import:")):
        return None, None
    for root in _roots(cfg):
        cand = (Path(root) / rel).resolve()
        try:
            if Path(root) in cand.parents or cand == Path(root):
                if cand.is_file():
                    return str(cand), root
        except Exception:
            continue
    return None, None


def indexed_sha(store, repo_id: str, rel: str) -> str | None:
    try:
        row = store.cx.execute(
            "SELECT sha256 FROM files WHERE path=?",
            (f"{repo_id}:{rel}",)).fetchone()
        return row["sha256"] if row else None
    except Exception:
        return None


def read_verified(store, cfg, repo_id: str, rel: str):
    """Returns (lines, sep, sha_ok, note). lines exclude line breaks; sep is
    the file's native separator for exact reassembly."""
    abs_path, _root = resolve_source(cfg, repo_id, rel)
    if abs_path is None:
        return None, "\n", False, "source-unavailable"
    try:
        raw = Path(abs_path).read_bytes()
    except Exception:
        return None, "\n", False, "source-unavailable"
    want = indexed_sha(store, repo_id, rel) or ""
    if want.startswith("sha256:"):
        want = want[len("sha256:"):]
    sha_ok = bool(want) and hashlib.sha256(raw).hexdigest() == want
    if not sha_ok:
        return None, "\n", False, "stale-source"
    try:
        text = raw.decode("utf-8")
    except Exception:
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return None, "\n", False, "decode-error"
    sep = "\r\n" if "\r\n" in text else "\n"
    return text.split(sep), sep, True, ""


def slice_span(lines: list[str], sep: str, start: int, end: int,
               max_lines: int = MAX_SPAN_LINES,
               max_bytes: int = MAX_SPAN_BYTES) -> dict:
    """Slice 1-indexed inclusive [start, end] with explicit truncation."""
    n = len(lines)
    start = max(start, 1)
    end = min(max(end, start), n)
    if start > n:
        return {"text": "", "start_line": start, "end_line": end,
                "truncated": True, "note": "range-beyond-eof"}
    chunk = lines[start - 1:end]
    truncated = False
    if len(chunk) > max_lines:
        chunk = chunk[:max_lines]
        truncated = True
    text = sep.join(chunk)
    if len(text.encode("utf-8", errors="ignore")) > max_bytes:
        raw = text.encode("utf-8", errors="ignore")[:max_bytes]
        text = raw.decode("utf-8", errors="ignore")
        truncated = True
    return {"text": text, "start_line": start,
            "end_line": start + len(chunk) - 1 if chunk else start,
            "truncated": truncated,
            "note": "truncated-to-limits" if truncated else ""}


def spanify_response(res: dict, store, cfg) -> dict:
    """Span-first transform of a legacy query_graph response (in place on a
    copy). Facts gain span_* fields from revision-verified source; evidence
    quotes are blanked ONLY where a verified span exists (recoverable via
    expand_evidence), otherwise kept with stale marking. Returns the new
    response dict (legacy input untouched)."""
    import copy
    out = copy.deepcopy(res)
    facts = out.get("facts")
    if not isinstance(facts, list):
        return out
    file_cache: dict[tuple[str, str], tuple] = {}
    spanned_files: set[str] = set()

    def _lines_for(repo_id: str, rel: str):
        key = (repo_id, rel)
        if key not in file_cache:
            file_cache[key] = read_verified(store, cfg, repo_id, rel)
        return file_cache[key]

    for fact in facts:
        if not isinstance(fact, dict) or not fact.get("id"):
            continue
        try:
            node = store.get_node(fact["id"])
        except Exception:
            node = None
        # Wire rules (documented defaults keep responses small): facts
        # without an established span carry only span_note (the legacy
        # quote stays intact); successful spans carry text/range plus
        # has_more, and only non-default flags beyond that.
        if node is None:
            fact.update({"span_note": "node-unavailable"})
            continue
        ntype = getattr(node, "type", "")
        loc = getattr(node, "location", None)
        repo = getattr(node, "repository_id", "")
        rel = getattr(node, "file_path", "")
        if ntype not in SYMBOL_SPAN_TYPES or loc is None or not rel or not repo:
            fact.update({"span_note": "non-symbol-fact"})
            continue
        start, end = getattr(loc, "start_line", 0), getattr(loc, "end_line", 0)
        if not start or end < start:
            fact.update({"span_note": "degenerate-range"})
            continue
        lines, sep, ok, note = _lines_for(repo, rel)
        if not ok or lines is None:
            fact.update({"span_start": start, "span_end": end,
                         "span_note": note})
            if note == "stale-source":
                fact["span_stale"] = True
            continue
        if ntype in ("function", "method", "test"):
            # Decorators are part of the definition construct: extend up
            # over contiguous @-lines (bounded, deterministic).
            while start > 1 and lines[start - 2].lstrip().startswith("@"):
                start -= 1
        part = slice_span(lines, sep, start, end)
        # Default-true/false flags are omitted when default (documented
        # defaults: complete=true, stale=false, note=""); has_more is the
        # expansion affordance and always present.
        fact.update({"span_text": part["text"], "span_start": part["start_line"],
                     "span_end": part["end_line"],
                     "span_has_more": bool(part["truncated"] or part["start_line"] > 1
                                           or part["end_line"] < len(lines))})
        if part["truncated"]:
            fact["span_complete"] = False
        if part["note"]:
            fact["span_note"] = part["note"]
        spanned_files.add(rel)
    if spanned_files and isinstance(out.get("evidence"), list):
        for row in out["evidence"]:
            if isinstance(row, dict) and row.get("quote"):
                row["quote"] = None
                row["span_evidence"] = True
    return out


def expand_evidence(store, cfg, evidence_id: str | None = None,
                    node_id: str | None = None, level: str = "span",
                    context_lines: int = CONTEXT_LINES) -> dict:
    """Read-only expansion by canonical identity. Levels: span (recorded
    range), context (span +/- lines), symbol (containing symbol range),
    file (whole file, bounded). Never fabricates: stale/missing sources
    return explicit errors with last-known metadata, plus the indexed quote
    when the evidence row carries one."""
    if level not in ("span", "context", "symbol", "file"):
        return {"error": f"unsupported level: {level!r}",
                "levels": ["span", "context", "symbol", "file"]}
    node = None
    if node_id:
        try:
            node = store.get_node(node_id)
        except Exception:
            node = None
        if node is None:
            return {"error": f"unknown node: {node_id!r}"}
    elif evidence_id:
        try:
            row = store.cx.execute(
                "SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        except Exception:
            row = None
        if row is None:
            return {"error": f"unknown evidence: {evidence_id!r}"}
        d = dict(row)
        # Anchor the row to a repository via the files table (the row itself
        # carries no repo): one indexed LIKE scan over file keys, not a walk
        # over every node. Shared file-level evidence resolves to its single
        # file; genuinely cross-repo sharing is refused, never guessed.
        try:
            rids = sorted({r["path"].split(":", 1)[0] for r in store.cx.execute(
                "SELECT path FROM files WHERE path LIKE ?",
                ("%:" + str(d.get("source_uri") or ""),)).fetchall()})
        except Exception:
            rids = []
        rel = str(d.get("source_uri") or "")
        if len(rids) != 1 or not rel:
            if not rids:
                # No indexed file to pin the repo: serve the indexed quote.
                return {"evidence_id": evidence_id, "level": level,
                        "text": d.get("quote"),
                        "start_line": d.get("start_line"),
                        "end_line": d.get("end_line"), "complete": False,
                        "truncated": False, "stale": False,
                        "note": "indexed-quote-only"}
            return {"error": "ambiguous evidence: spans several repositories",
                    "candidates": rids[:10]}
        repo = rids[0]
        start0, end0 = d.get("start_line") or 0, d.get("end_line") or 0
        lines, sep, ok, note = read_verified(store, cfg, repo, rel)
        if not ok or lines is None:
            return {"evidence_id": evidence_id, "level": level,
                    "text": d.get("quote"),
                    "start_line": d.get("start_line"),
                    "end_line": d.get("end_line"), "complete": False,
                    "truncated": False, "stale": True, "note": note,
                    "file": rel}
        if level == "file" or not start0:
            part = slice_span(lines, sep, 1, len(lines),
                              max_bytes=MAX_EXPAND_BYTES)
        elif level == "context":
            part = slice_span(lines, sep, start0 - context_lines,
                              end0 + context_lines, max_bytes=MAX_EXPAND_BYTES)
        elif level == "symbol":
            s, e = _containing_symbol(store, repo, rel, start0, end0)
            part = slice_span(lines, sep, s, e, max_bytes=MAX_EXPAND_BYTES)
        else:  # span
            part = slice_span(lines, sep, start0, end0,
                              max_bytes=MAX_EXPAND_BYTES)
        return {"evidence_id": evidence_id, "level": level,
                "text": part["text"], "start_line": part["start_line"],
                "end_line": part["end_line"],
                "complete": level == "file" and not part["truncated"],
                "truncated": part["truncated"], "stale": False,
                "note": part["note"], "file": rel}
    else:
        return {"error": "pass node_id or evidence_id"}
    repo = getattr(node, "repository_id", "")
    rel = getattr(node, "file_path", "")
    loc = getattr(node, "location", None)
    if not repo or not rel or loc is None:
        return {"error": "node has no source location",
                "node_id": getattr(node, "id", "")}
    lines, sep, ok, note = read_verified(store, cfg, repo, rel)
    if not ok or lines is None:
        fallback = None
        if evidence_id:
            try:
                row = store.cx.execute(
                    "SELECT quote FROM evidence WHERE id=?", (evidence_id,)).fetchone()
                fallback = row["quote"] if row else None
            except Exception:
                fallback = None
        return {"node_id": getattr(node, "id", ""), "level": level,
                "text": fallback, "complete": False, "truncated": False,
                "stale": True, "note": note or "source-unavailable",
                "file": rel}
    start, end = getattr(loc, "start_line", 1), getattr(loc, "end_line", 1)
    if level == "context":
        start, end = start - context_lines, end + context_lines
    elif level == "symbol":
        start, end = _containing_symbol(store, repo, rel, start, end)
    elif level == "file":
        start, end = 1, len(lines)
    part = slice_span(lines, sep, start, end, max_bytes=MAX_EXPAND_BYTES)
    return {"node_id": getattr(node, "id", ""),
            "evidence_id": evidence_id, "level": level,
            "text": part["text"], "start_line": part["start_line"],
            "end_line": part["end_line"], "complete": level != "context" and not part["truncated"],
            "truncated": part["truncated"], "stale": False,
            "note": part["note"], "file": rel}


def _containing_symbol(store, repo: str, rel: str, start: int, end: int):
    """Smallest same-file symbol range strictly containing [start, end];
    falls back to the span itself when none qualifies."""
    best = None
    try:
        rows = store.cx.execute(
            "SELECT id FROM file_nodes WHERE file_key=?", (f"{repo}:{rel}",)).fetchall()
        for r in rows:
            try:
                n = store.get_node(r["node_id"])
            except Exception:
                continue
            if n is None or getattr(n, "file_path", "") != rel:
                continue
            loc = getattr(n, "location", None)
            if loc is None:
                continue
            s, e = getattr(loc, "start_line", 0), getattr(loc, "end_line", 0)
            if not s or e < s or (s, e) == (start, end):
                continue
            if s <= start and end <= e:
                area = e - s
                if best is None or area < best[2]:
                    best = (s, e, area)
    except Exception:
        pass
    if best is None:
        return start, end
    return best[0], best[1]
