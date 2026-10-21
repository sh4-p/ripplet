"""compare_revisions: what changed between two commits, symbol by symbol.

Both sides come from git objects (`git show <rev>:<path>`), parsed with
the same extractors as the index, so the answer is evidence recorded in
the repository, not a reconstruction:

  * the revisions as resolved SHAs, dates and subjects,
  * per changed file: symbols added, removed, renamed (same body, new
    name) and modified (body changed), with signature changes noted,
  * the commits in between that touched each file.

Relations at the old revision (who called what then) are not rebuilt:
the current graph describes the working tree only, and the answer says
which side each fact comes from.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path

from ..testplan.changes import _git

_SYM = ("function", "method", "class", "interface", "struct", "type", "trait", "enum", "record", "test",
        "table", "column")
MAX_FILES = 200


def _rev(root: str, ref: str) -> dict | None:
    code, out = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if code != 0 or not out.strip():
        return None
    sha = out.strip()
    code, info = _git(root, "log", "-1", "--format=%H%x1f%ad%x1f%an%x1f%s", "--date=short", sha)
    parts = (info.strip().split("\x1f") + ["", "", "", ""])[:4]
    return {"ref": ref, "sha": sha, "date": parts[1], "author": parts[2], "subject": parts[3]}


def _symbols(root: str, rev: str, rel: str, tmp: Path, repo_id: str) -> dict | None:
    """qualified name -> {type, line, sig, body} at `rev`, or None if absent."""
    code, text = _git(root, "show", f"{rev}:{rel}")
    if code != 0:
        return None
    from ..extraction.pipeline import extract_file
    p = tmp / rev[:12] / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    try:
        res = extract_file(str(p), rel, repo_id, "rev")
    except Exception:
        return {}
    lines = text.splitlines()
    out = {}
    for n in res.nodes:
        if n.type not in _SYM:
            continue
        loc = n.location
        a = getattr(loc, "start_line", 0) or 0
        b = max(a, getattr(loc, "end_line", 0) or a)
        body = "\n".join(x.strip() for x in lines[a - 1:b]) if a else ""
        attrs = n.attributes or {}
        sig = (attrs.get("params_min"), attrs.get("params_max"), attrs.get("returns") or attrs.get("type") or "")
        head = lines[a - 1].strip() if 1 <= a <= len(lines) else ""
        inner = "\n".join(x.strip() for x in lines[a:b]) if a else ""

        def h(t: str) -> str:
            return hashlib.sha1(re.sub(r"\s+", " ", t).encode()).hexdigest()
        # `inner` (body without the declaration line) identifies a rename;
        # `body` (with it) a modification; the declaration line a signature
        out[n.qualified_name or n.label] = {"type": n.type, "line": a, "sig": sig, "head": head[:140],
                                            "body": h(body), "inner": h(inner) if b > a else ""}
    return out


def compare_revisions(cfg, base: str, head: str = "HEAD", path: str = "") -> str:
    roots = [str(r) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    out = []
    for root in roots:
        a, b = _rev(root, base), _rev(root, head)
        if a is None or b is None:
            missing = base if a is None else head
            out.append(f"compare_revisions: `{missing}` is not a commit in {root}. "
                       "Use a branch, tag or SHA that `git rev-parse` knows.")
            continue
        out.append(f"# compare_revisions {a['sha'][:10]}..{b['sha'][:10]}")
        out.append(f"base: {a['ref']} = {a['sha'][:10]} ({a['date']}, {a['author']}) {a['subject']}")
        out.append(f"head: {b['ref']} = {b['sha'][:10]} ({b['date']}, {b['author']}) {b['subject']}")
        code, lg = _git(root, "log", "--format=%h", f"{a['sha']}..{b['sha']}")
        if code == 0:
            out.append(f"{len([x for x in lg.splitlines() if x.strip()])} commit(s) from base to head.")
        args = ["diff", "--name-status", "-M", a["sha"], b["sha"]]
        if path:
            args += ["--", path]
        code, diff = _git(root, *args)
        if code != 0:
            out.append("git diff failed.")
            continue
        entries = []
        for ln in diff.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2:
                st = parts[0][:1]
                old, new = (parts[1], parts[2]) if st == "R" and len(parts) >= 3 else (parts[1], parts[1])
                entries.append((st, old, new))
        out.append(f"{len(entries)} file(s) changed. Facts below are read from the git objects of both commits; "
                   "callers/callees are not rebuilt for the base (the index describes the working tree).")
        out.append("")
        totals = {"added": 0, "removed": 0, "modified": 0, "renamed": 0}
        with tempfile.TemporaryDirectory(prefix="ripplet-rev-") as td:
            tmp = Path(td)
            for st, old, new in entries[:MAX_FILES]:
                before = _symbols(root, a["sha"], old, tmp, "rev") if st != "A" else {}
                after = _symbols(root, b["sha"], new, tmp, "rev") if st != "D" else {}
                before, after = before or {}, after or {}
                label = {"A": "added", "D": "deleted", "M": "modified", "R": f"renamed from {old}"}.get(st, st)
                added = sorted(set(after) - set(before))
                removed = sorted(set(before) - set(after))
                common = sorted(set(before) & set(after))
                modified = [q for q in common if before[q]["body"] != after[q]["body"]]
                # same body under a new name: a rename, not an add + a remove
                renamed = []
                by_body = {before[q]["inner"]: q for q in removed if before[q]["inner"]}
                for q in list(added):
                    old_q = by_body.get(after[q]["inner"]) if after[q]["inner"] else None
                    if old_q:
                        renamed.append((old_q, q))
                        added.remove(q)
                        removed.remove(old_q)
                # a type whose body changed only because a member did is not news
                members = set(added) | set(removed) | set(modified) | {new_q for _old_q, new_q in renamed}
                modified = [q for q in modified
                            if not any(m.startswith(q + ".") for m in members)]
                if not (added or removed or modified or renamed) and st == "M":
                    out.append(f"-- {new} ({label}): no symbol-level change (formatting, comments or data)")
                    continue
                out.append(f"-- {new} ({label})")
                for q in added:
                    out.append(f"   + {after[q]['type']} {q}  @{after[q]['line']}: {after[q]['head']}")
                for q in removed:
                    out.append(f"   - {before[q]['type']} {q}  (was @{before[q]['line']}): {before[q]['head']}")
                for o, n in renamed:
                    out.append(f"   ~ renamed {o} -> {n}  @{after[n]['line']}")
                for q in modified:
                    sig = "" if before[q]["head"] == after[q]["head"] else \
                        f"  [signature {before[q]['head']}  ->  {after[q]['head']}]"
                    out.append(f"   * {after[q]['type']} {q}  @{after[q]['line']} body changed{sig}")
                totals["added"] += len(added)
                totals["removed"] += len(removed)
                totals["modified"] += len(modified)
                totals["renamed"] += len(renamed)
                code, log = _git(root, "log", "--format=%h %ad %s", "--date=short", "-n", "5",
                                 f"{a['sha']}..{b['sha']}", "--", new)
                if code == 0 and log.strip():
                    out.append("   commits: " + " | ".join(x.strip() for x in log.strip().splitlines()))
        if len(entries) > MAX_FILES:
            out.append(f"... {len(entries) - MAX_FILES} more files not compared (pass path=<dir> to narrow).")
        out.insert(4, f"Symbols: {totals['added']} added, {totals['removed']} removed, "
                      f"{totals['renamed']} renamed, {totals['modified']} modified.")
    return "\n".join(out)
