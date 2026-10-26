"""Bounded tool output.

Every tool answer is fitted to a token budget before it reaches the agent:
a per-tool default, or the caller's `budget` argument. A result that does
not fit is shortened in a way the agent can act on:

  * text keeps whole lines from the top (tools put the most important part
    first), lists the section headings that were cut, and ends with one
    `[ripplet: ...]` line saying exactly how to get the rest;
  * JSON is shrunk structurally (longest lists halved, very long strings
    clipped) so it stays valid JSON, with a `_truncated` map of what was cut.

Nothing is ever cut silently.
"""
from __future__ import annotations

import json

CHARS_PER_TOKEN = 3.5          # code-heavy text; conservative vs the usual 4
HARD_CAP_CHARS = 80_000        # absolute ceiling, whatever budget is asked for
MIN_BUDGET = 300

DEFAULT_BUDGET = 6000
TOOL_BUDGETS = {
    "check_change": 6000,
    "change_plan": 6000,
    "rename_symbol": 4000,
    "change_signature": 4000,
    "compare_revisions": 6000,
    "repo_map": 3500,
    "task_context": 4500,
    "investigate_error": 3000,
    "get_architecture_summary": 3500,
    "feature_slice": 10000,
    "list_routes": 6000,
    "explain_symbol": 4000,
    "read_code": 9000,
    "edit_code": 3000,
    "write_code": 500,
    "query_graph": 4000,
    "search_symbols": 2500,
    "get_neighbors": 3000,
    "analyze_change_impact": 4000,
    "find_related_tests": 3000,
    "find_path": 3000,
    "expand_evidence": 5000,
}

HINTS = {
    "check_change": "fix the DEFINITE items first and call check_change again, or pass depth=1",
    "investigate_error": "paste only the part of the trace you care about",
    "task_context": "use the specific names from the task, or explain_symbol on one symbol listed above",
    "repo_map": "call repo_map(focus='<directory>') for one area in full",
    "get_architecture_summary": "call repo_map(focus='<directory>') for one area in full",
    "compare_revisions": "pass path=<directory> to compare one area",
    "change_plan": "every CALL SITE above is complete for the files shown; for the rest call "
                   "change_plan again with a larger budget",
    "feature_slice": "call feature_slice again with focus='<part of a path listed under Not shown>' "
                     "to get those files, or a larger budget",
    "explain_symbol": "use expand_evidence(node_id=<id>, level='symbol') for the full source, "
                      "get_neighbors(id, direction='in', offset=<n>) for more callers",
    "query_graph": "ask a more specific question or pass a smaller limit",
    "search_symbols": "use a more specific name or a smaller limit",
    "get_neighbors": "pass relations=[...] or offset=<n> to page",
    "analyze_change_impact": "pass fewer symbols or max_depth=1",
    "find_related_tests": "pass offset=<n> to page, or fewer symbols",
    "find_path": "pass a smaller max_depth",
    "expand_evidence": "use level='span' or 'context' instead of 'file'",
}


def budget_for(name: str, args: dict | None) -> int:
    """Token budget for this call: caller's `budget` (clamped) or the tool default."""
    raw = (args or {}).get("budget")
    try:
        b = int(raw) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        b = 0
    if b <= 0:
        b = TOOL_BUDGETS.get(name, DEFAULT_BUDGET)
        syms = (args or {}).get("symbols")
        if name == "explain_symbol" and isinstance(syms, list) and len(syms) > 1:
            b *= min(len(syms), 4)      # one call for several names must not be cut to one name's room
    return max(MIN_BUDGET, b)


def char_cap(tokens: int) -> int:
    return min(HARD_CAP_CHARS, int(tokens * CHARS_PER_TOKEN))


def _notice(name: str, shown: int, total: int, tokens: int, extra: str = "") -> str:
    hint = HINTS.get(name, "narrow the request")
    more = min(int(total / CHARS_PER_TOKEN) + 200, int(HARD_CAP_CHARS / CHARS_PER_TOKEN))
    s = (f"[ripplet: shortened to ~{tokens} tokens ({shown:,} of {total:,} chars){extra}. "
         f"To see the rest: {hint}")
    if more > tokens:
        s += f"; or pass budget={more} for everything"
    return s + ".]"


def _is_heading(line: str) -> bool:
    s = line.strip()
    return bool(s) and (s.startswith(("===", "## ", "# ")) or
                        (s.isupper() and len(s) > 3 and s[0].isalpha()))


def fit_text(text: str, name: str = "", tokens: int = DEFAULT_BUDGET) -> str:
    cap = char_cap(tokens)
    if len(text) <= cap:
        return text
    lines = text.split("\n")
    room = cap - 400            # space for the notice and the cut-heading list
    kept, used, i = [], 0, 0
    while i < len(lines) and used + len(lines[i]) + 1 <= room:
        kept.append(lines[i]); used += len(lines[i]) + 1; i += 1
    if not kept:                # one giant line: cut inside it
        kept, i = [lines[0][:max(room, 0)]], 1
    rest = lines[i:]
    cut_heads = [l.strip() for l in rest if _is_heading(l)]
    extra = f"; {len(rest)} lines cut"
    out = "\n".join(kept)
    if cut_heads:
        shown_heads = cut_heads[:12]
        tail = f" (+{len(cut_heads) - 12} more)" if len(cut_heads) > 12 else ""
        out += "\n[ripplet: sections not shown: " + " | ".join(h[:80] for h in shown_heads) + tail + "]"
    return out + "\n" + _notice(name, len(out), len(text), tokens, extra)


def _largest_list(obj, path=()):
    """(size, path, list) of the serialized-largest list with >1 item."""
    best = None
    if isinstance(obj, list):
        if len(obj) > 1:
            best = (len(json.dumps(obj, default=str)), path, obj)
        for k, v in enumerate(obj):
            c = _largest_list(v, path + (k,))
            if c and (best is None or c[0] > best[0]):
                best = c
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k == "_truncated":
                continue
            c = _largest_list(v, path + (k,))
            if c and (best is None or c[0] > best[0]):
                best = c
    return best


def _clip_strings(obj, limit: int):
    if isinstance(obj, str):
        return obj if len(obj) <= limit else obj[:limit] + f"...[{len(obj) - limit} chars cut]"
    if isinstance(obj, list):
        return [_clip_strings(v, limit) for v in obj]
    if isinstance(obj, dict):
        return {k: _clip_strings(v, limit) for k, v in obj.items()}
    return obj


def fit_json(obj, name: str = "", tokens: int = DEFAULT_BUDGET) -> str:
    cap = char_cap(tokens)
    text = json.dumps(obj, default=str)
    if len(text) <= cap:
        return text
    total = len(text)
    work = json.loads(text)     # plain copy; default=str already applied
    cut: dict = {}
    for _ in range(60):
        if len(json.dumps(work)) <= cap - 300:
            break
        big = _largest_list(work)
        if big is None:
            break
        _, path, lst = big
        key = "/".join(map(str, path)) or "$"
        orig = cut.get(key, (None, len(lst)))[1]
        keep = max(1, len(lst) // 2)
        del lst[keep:]
        cut[key] = (keep, orig)
    limit = 4000
    while len(json.dumps(work)) > cap - 300 and limit >= 200:
        work = _clip_strings(work, limit)
        limit //= 2
    note = {k: f"kept {a} of {b}" for k, (a, b) in cut.items()}
    hint = _notice(name, len(json.dumps(work, default=str)), total, tokens)
    if isinstance(work, dict):
        work["_truncated"] = {"lists": note, "how_to_get_more": hint}
    else:
        work = {"results": work, "_truncated": {"lists": note, "how_to_get_more": hint}}
    out = json.dumps(work, default=str)
    if len(out) > cap:          # pathological shape: last resort, still announced
        out = out[:cap] + "\n" + hint
    return out


def fit(out, name: str = "", args: dict | None = None) -> str:
    tokens = budget_for(name, args)
    if isinstance(out, str):
        return fit_text(out, name, tokens)
    return fit_json(out, name, tokens)


# Advertised only where answers get long; every tool still honours `budget`.
# Kept terse: tool schemas are re-sent to the model on every turn.
ADVERTISED = ("task_context", "repo_map", "check_change", "change_plan", "feature_slice", "explain_symbol", "query_graph",
              "get_neighbors", "expand_evidence", "analyze_change_impact")
BUDGET_PROP = {"type": "integer", "description": "Max answer tokens"}


def add_budget_param(tool_defs: list) -> None:
    for t in tool_defs:
        if t["name"] in ADVERTISED:
            t.setdefault("inputSchema", {}).setdefault("properties", {}).setdefault("budget", dict(BUDGET_PROP))
