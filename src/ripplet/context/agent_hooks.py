"""Hook entry point for coding agents: steer code-structure lookups to the graph.

    python -m ripplet.context.agent_hooks claude [--strict]   Claude Code / Codebuddy PreToolUse
    python -m ripplet.context.agent_hooks claude-post          PostToolUse on mcp__ripplet__*: stamps use
    python -m ripplet.context.agent_hooks gemini               Gemini CLI BeforeTool

Per agent session (Claude Code also sends `agent_id` for subagents):

* a search for a name that IS an indexed symbol gets a pointed hint (its
  definition, its caller count and the tool that answers in one call);
* the first raw search/read of source code, before any Ripplet tool was used,
  gets one reminder of which tool answers what;
* with --strict, raw reads/searches of source are DENIED until a Ripplet
  tool has run (at most MAX_DENIES times, then allowed: an agent can never
  be stranded), and afterwards a search for a name that is an indexed symbol
  is denied once per pattern with the pointed tool; repeating the same
  search goes through (text matches stay possible).

Fails open everywhere: any error prints nothing and the tool call proceeds.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from . import rules as _rules

SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".kts", ".go", ".rs", ".cs",
              ".php", ".rb", ".lua", ".swift", ".dart", ".c", ".h", ".cc", ".cpp", ".hpp", ".scala", ".vue",
              ".svelte", ".sql"}
REMINDER = (
    "Ripplet: this repository has a code graph. Before grepping or reading files for code structure, use "
    "it: task_context (start of a task), explain_symbol (a known name: source + callers + callees), "
    "get_neighbors / analyze_change_impact (who calls X, what breaks), list_routes (endpoints and their "
    "access checks), rename_symbol (does a whole rename in one call), change_plan (before a signature change/removal), feature_slice (a feature like an "
    "existing one), check_change (after editing). Read files only for lines a tool pointed to. "
    "Text (strings, config keys, messages) is still a job for grep.")
LOAD = ("The tools are the MCP server `ripplet`: mcp__ripplet__task_context, mcp__ripplet__explain_symbol, "
        "mcp__ripplet__get_neighbors, mcp__ripplet__analyze_change_impact, mcp__ripplet__change_plan, "
        "mcp__ripplet__rename_symbol, "
        "mcp__ripplet__check_change. If they are deferred, load them with ToolSearch query "
        "\"select:mcp__ripplet__task_context,mcp__ripplet__explain_symbol,mcp__ripplet__get_neighbors,"
        "mcp__ripplet__change_plan,mcp__ripplet__rename_symbol,mcp__ripplet__check_change\".")
DENY = ("Ripplet (strict): no Ripplet tool was used yet in this session, so raw reads/searches of source "
        "code are blocked. " + REMINDER.split(": ", 1)[1] + " " + LOAD +
        " Call one of them first; reads go through once a Ripplet tool has run.")
MAX_DENIES = 3


def _state_dir(root: Path) -> Path | None:
    db = _rules._db(root)
    return None if db is None else db.parent / "hook_state"


def _key(ev: dict) -> str:
    s = f"{ev.get('session_id') or 'nosession'}-{ev.get('agent_id') or 'main'}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)[:120]


def _load(sd: Path, key: str) -> dict:
    try:
        return json.loads((sd / f"guard-{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(sd: Path, key: str, st: dict) -> None:
    try:
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"guard-{key}.json").write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass


def _targets_source(tool: str, tin: dict, root: Path) -> bool:
    """A read/search of the repository's source code (not docs, config, or files elsewhere)."""
    if tool in ("Grep", "Bash"):
        return _rules.search_pattern(tool, tin) is not None
    vals = [str(tin.get(k) or "") for k in ("file_path", "path", "pattern")]
    exts = {Path(v.replace("\\", "/").split("*")[-1]).suffix.lower() for v in vals if v}
    if not exts & SOURCE_EXT and tool != "Glob":
        return False
    fp = str(tin.get("file_path") or tin.get("path") or "")
    if fp and Path(fp).is_absolute():
        try:
            Path(fp).resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return False                      # outside the project: the graph has nothing to say
    return tool != "Glob" or bool(exts & SOURCE_EXT)


def claude(ev: dict, strict: bool) -> dict | None:
    tool = ev.get("tool_name") or ""
    tin = ev.get("tool_input") or {}
    if not isinstance(tin, dict) or tool not in ("Read", "Grep", "Glob", "Bash"):
        return None
    root = Path(ev.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ".")
    sd = _state_dir(root)
    if sd is None or not _targets_source(tool, tin, root):
        return None
    key = _key(ev)
    st = _load(sd, key)
    if st.get("used"):
        # the agent already works with the graph: only the pointed symbol hint
        pointed = _rules.handle(ev) if tool in ("Grep", "Bash") else None
        if strict and pointed:
            # a grep for an indexed symbol: the graph answers it (callers through
            # interfaces/DI, no same-named noise). Denied once per pattern; the
            # same search again goes through for genuine text matches.
            pat = _rules.search_pattern(tool, tin) or ""
            seen = st.setdefault("symbol_denied", [])
            if pat not in seen:
                seen.append(pat)
                _save(sd, key, st)
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": "Ripplet (strict): " +
                    pointed["hookSpecificOutput"]["additionalContext"].replace(
                        " This search still runs as asked.", "") +
                    " Use it instead of grep; if you need plain text matches anyway, run the same search again."}}
        return pointed
    if strict and int(st.get("denies", 0)) < MAX_DENIES and tool in ("Read", "Grep", "Glob", "Bash"):
        st["denies"] = int(st.get("denies", 0)) + 1
        st["denied"] = time.time()
        _save(sd, key, st)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": DENY}}
    pointed = _rules.handle(ev) if tool in ("Grep", "Bash") else None
    if st.get("reminded"):
        return pointed
    st["reminded"] = time.time()
    _save(sd, key, st)
    msg = REMINDER
    if pointed:
        msg += " " + pointed["hookSpecificOutput"]["additionalContext"]
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}}


def claude_post(ev: dict) -> None:
    """A Ripplet tool ran: this agent is oriented, stop reminding/blocking it."""
    root = Path(ev.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ".")
    sd = _state_dir(root)
    if sd is None:
        return
    key = _key(ev)
    st = _load(sd, key)
    st["used"] = time.time()
    _save(sd, key, st)


def gemini(ev: dict) -> dict:
    """Gemini CLI BeforeTool: always allow; remind once per hour per project."""
    out = {"decision": "allow"}
    root = Path(ev.get("cwd") or os.getcwd()) if isinstance(ev, dict) else Path(os.getcwd())
    sd = _state_dir(root)
    if sd is None:
        return out
    st = _load(sd, "gemini")
    if time.time() - float(st.get("reminded") or 0) > 3600:
        st["reminded"] = time.time()
        _save(sd, "gemini", st)
        out["additionalContext"] = REMINDER
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    kind = argv[0] if argv else "claude"
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "ignore") if not sys.stdin.isatty() else ""
        ev = json.loads(raw or "{}")
        if not isinstance(ev, dict):
            ev = {}
        if kind == "claude":
            res = claude(ev, strict="--strict" in argv or os.environ.get("RIPPLET_GUARD_STRICT") == "1")
            if res is not None:
                sys.stdout.write(json.dumps(res))
        elif kind == "claude-post":
            claude_post(ev)
        elif kind == "gemini":
            sys.stdout.write(json.dumps(gemini(ev)))
    except Exception:
        if kind == "gemini":
            sys.stdout.write(json.dumps({"decision": "allow"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
