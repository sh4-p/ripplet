"""Possible call sites Ripplet could not verify.

A graph that answers "who calls X" must not hide what it is unsure about: a
call `x.add_item()` whose receiver `x` has no type Ripplet can establish is
either a call of X or of something else named the same. Such sites are never
turned into edges (that would be guessing); they are listed beside the proven
callers so an agent checks them instead of trusting a silently short list.

Kept: unresolved calls of the same member name whose receiver is untyped,
not bound to any symbol on that line.
Dropped: lines where some symbol of that name is already bound (ours or
another), receivers typed as something that is not ours (an external type's
method), names imported from an external package.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..change.check import occurrence_lines

TOO_COMMON = 60     # above this, a name is too common to list site by site


def _code_at(roots, rel, line) -> str:
    for r in roots:
        p = Path(r) / rel
        if p.is_file():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return ""
            if 1 <= line <= len(lines):
                t = lines[line - 1].strip()
                return t if len(t) <= 140 else t[:137] + "..."
    return ""


def _external_import(a: dict) -> bool:
    b = a.get("import_binding")
    if isinstance(b, dict):
        spec = str(b.get("specifier") or "")
        return bool(spec) and not spec.startswith((".", "/", "~", "@/"))
    return False


def unverified_sites(cx, roots, ids, name: str, skip: set | None = None, limit: int = 15):
    """([(file, line, code, receiver)], total) of possible, unproven calls of
    `name` for the symbols `ids` (a symbol and its override family)."""
    if not name or len(name) < 3:
        return [], 0
    skip = set(skip or ())
    ids = set(ids)
    labels = {r[0]: r[1] for r in cx.execute("SELECT id, label FROM nodes WHERE label=?", (name,))}
    # lines where some symbol named `name` is already bound: not uncertain
    bound: set = set()
    q = ",".join("?" * len(labels)) if labels else "''"
    for fp, ln, attrs in cx.execute(
            f"SELECT file_path, start_line, attributes FROM edges WHERE relation IN "
            f"('calls','instantiates','references','dispatches') AND target IN ({q})", tuple(labels)):
        for x in occurrence_lines(ln or 0, attrs):
            bound.add(((fp or "").replace("\\", "/"), x))
    out, seen = [], set()
    for fp, ln, attrs, tgt in cx.execute(
            "SELECT file_path, start_line, attributes, target FROM edges WHERE relation='calls_unresolved' "
            "AND (target LIKE ? OR attributes LIKE ?) "
            "AND id NOT IN (SELECT edge_id FROM superseded_edges)",
            ("%" + name, '%"member": "' + name + '"%')):
        try:
            a = json.loads(attrs or "{}")
        except ValueError:
            a = {}
        member = a.get("member") or ""
        spelled = str(a.get("unresolved_call") or tgt or "")
        last = spelled.rsplit(".", 1)[-1].rsplit(":", 1)[-1]
        if member != name and last != name:
            continue
        if not (a.get("base") or "." in spelled.split(":")[-1] or a.get("callee_kind") in ("complex", "receiver",
                                                                                          "member", "selector")):
            continue                     # a bare `f()` the resolvers would have bound if it were ours
        rt = str(a.get("receiver_type") or "")
        if rt and not rt.startswith(("()", "=")):
            continue                     # typed as something that is not ours
        if _external_import(a):
            continue
        f = (fp or "").replace("\\", "/")
        recv = str(a.get("base") or spelled.rsplit(".", 1)[0].split(":")[-1])
        for x in occurrence_lines(ln or 0, attrs):
            k = (f, x)
            if k in bound or k in skip or k in seen:
                continue
            seen.add(k)
            out.append((f, x, recv))
    out.sort()
    total = len(out)
    if total > TOO_COMMON:
        # `.get(` / `.close(` on untyped receivers everywhere: listing them is
        # noise; the per-file counts say where to look
        by_file: dict = {}
        for f, _x, _r in out:
            by_file[f] = by_file.get(f, 0) + 1
        top = sorted(by_file.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return [(f, 0, f"{n} untyped `.{name}(` call(s)", "") for f, n in top], total
    return [(f, x, _code_at(roots, f, x), r) for f, x, r in out[:limit]], total


def render(sites, total: int, name: str) -> list:
    """Lines for a text answer (empty when there is nothing to check)."""
    if not total:
        return []
    if total > TOO_COMMON:
        out = ["", f"=== UNVERIFIED ({total}) - `{name}` is a common member name: {total} calls on receivers "
                   "whose type Ripplet could not establish (most are other types' `" + name + "`). Not counted "
                   "above. Files with the most such calls:"]
        for f, _ln, code, _r in sites:
            out.append(f"  {f}  ({code})")
        return out
    out = ["", f"=== UNVERIFIED ({total}) - `{name}` called on a receiver whose type Ripplet could not "
               "establish. Not counted above. For impact or a rename, treat each as a caller unless its "
               "code shows the receiver is a different type:"]
    for f, ln, code, recv in sites:
        out.append(f"  {f}:{ln}  {code}")
    if total > len(sites):
        out.append(f"  ... {total - len(sites)} more")
    return out
