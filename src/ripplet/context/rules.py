"""Always-on agent rules: tell the agent, in the files it always reads, when
the graph answers better than grep/read.

Two parts, both reversible with `ripplet rules off`:

  * A marked block in CLAUDE.md (Claude Code) and/or AGENTS.md (OpenCode,
    Codex and most other agents). Text outside the markers is never touched;
    re-running replaces the block in place.
  * Claude Code only: a PreToolUse hook on Grep and Bash. When the agent
    searches for a name that IS an indexed code symbol, it adds one line of
    context naming the symbol's definition and the tool that answers in one
    call. Text searches (strings, config keys, messages) get nothing: the
    graph has no better answer there, and a nudge would be noise. At most a
    few nudges per session, never twice for the same symbol, never blocks.
"""
from __future__ import annotations

import json
import re
import shlex
import sqlite3
import sys
from pathlib import Path

BEGIN = "<!-- ripplet:rules:begin -->"
END = "<!-- ripplet:rules:end -->"
NUDGE_MARK = "ripplet.context.rules"
MAX_NUDGES = 4

RULES = """## Ripplet (code graph of this repository)

This repository is indexed by Ripplet. Its tools answer code-structure
questions from a prebuilt graph, with file:line for every fact. They know
calls through interfaces, DI, overrides and imports that text search misses.

- If the request already carries a "Ripplet pre-fetched" block, that IS the
  `task_context` answer: start from its files and symbols, and skip `task_context`
  and `repo_map`. Ask `explain_symbol` / `get_neighbors` only for what it lacks.
- Starting a task without that block: call `task_context` with the task text
  first. It returns the code the task is about (with excerpts and real callers),
  related routes and tables, an existing feature to imitate for "like X", the
  files to edit and the tests that reach them.
- New to this repository: call `repo_map` first. It lists entry points and the
  files and symbols the rest of the code uses most, with signatures.
- A known name (class, function, method): call `explain_symbol` before Grep or
  Read. One call returns its source, every caller and every callee with file:line.
  Need several names? Pass them all in `symbols` in ONE call, not one call each.
- "Who calls X?" / "What breaks if X changes?": `get_neighbors` (direction=in) or
  `analyze_change_impact`. Do not grep for callers: grep misses indirect calls
  and returns unrelated code with the same name. The list is complete only
  when the answer has no `... more:` line, no `[inferred]` caller and no
  UNVERIFIED section. Otherwise fetch the rest (offset), then open each
  inferred / unverified line (a few lines of Read each) before reporting.
- A test failed or something crashed: pass the printed trace or error line to
  `investigate_error`. It maps each frame to its function and code, shows the
  code where it happened and its callers, and finds where the message is thrown.
- If a tool named below is not callable yet (deferred), load it first with ToolSearch
  `select:mcp__ripplet__rename_symbol,mcp__ripplet__move_symbol,mcp__ripplet__change_signature`
  (the names you need), together with the other tools you plan to use, in ONE ToolSearch call.
- Renaming a symbol: call `rename_symbol` (symbol, new_name). It edits the
  declaration, overrides, every call site and import itself, only on lines the
  graph proves, and returns the remaining text mentions to review. Do not open
  and edit each file by hand.
- Moving a top-level Python or TypeScript/JavaScript function, class, type or const to another module, a Java
  class to another package, a C# type to another file, a Go func/type/const/var to another file or package, a PHP class to another file, a Dart declaration to another file, a Rust item to another module file, or a Kotlin declaration to another file or package: call `move_symbol` (symbol, to_file). It moves the definition with the imports it needs and rewrites
  `from old import name` / `import {name} from "./old"` everywhere in one call; do not cut and paste by hand.
- Adding, removing or reordering parameters: call `change_signature` (symbol, add_param=
  {decl, value}, remove_param=name or reorder_params=[all names in the new order]). It edits the declaration, overrides and
  EVERY call site the graph proves in one call; editing call sites one by one
  costs a turn each.
  The files are already edited when it returns: do not Read or Edit them again,
  only check the sites it lists as NOT edited or unverified.
- Other signature changes, or removing a symbol: call `change_plan` BEFORE
  editing. It lists every line to change (overrides and each call site), and
  same-named code that must NOT change.
- Security / permission audits, "which endpoint lets a user do X": call
  `list_routes`. It lists every route with its handler and the access checks
  it calls (write routes without a check first) and proposes a
  ripplet.arch.yaml rule that keeps the check enforced on new routes.
- Database: tables and columns from .sql files and framework migrations
  (Laravel, Rails, Django, Alembic, EF Core, Prisma) are symbols too.
  `explain_symbol` / `change_plan` on `tasks` or `tasks.status_id` list the
  queries that use them; write a migration, then `check_change` lists code
  that still uses what it drops or renames.
- Adding a feature similar to an existing one: call `feature_slice` with the
  existing feature's name first. It returns that feature's files and every
  place the new feature must also touch.
- After editing code, before running the whole test suite: call `check_change`.
  It lists callers your edit broke, places that still use what you replaced,
  and the exact tests to run. Fix what it marks DEFINITE; POSSIBLE items are
  hints to look at, not orders. Pass `task` (the task in words) to also see
  files you changed that the task does not lead to. Architecture rules in
  ripplet.arch.yaml (`ripplet arch --example`) are checked there too.
- Read a file only for lines a tool result pointed to, or code it did not show.
- Use Grep for text: strings, config keys, log messages, comments. The graph
  indexes code symbols, not text.
- A result that was shortened ends with a line starting `[ripplet:` that says
  how to get the rest. Follow it instead of reading whole files.
- Answer code-structure questions (flows, callers, where X happens) with these
  tools yourself; an exploration subagent that greps and reads files costs
  more turns for the same answer. When you do hand exploration to a subagent,
  put these rules in its prompt.

Without the MCP tools, the same answers come from the shell, in the repository
root: `ripplet tool repo_map`, `ripplet tool explain_symbol symbol=<Name>`,
`ripplet tool rename_symbol symbol=<Name> new_name=<New>`,
`ripplet tool change_plan symbol=<Name> change=signature`,
`ripplet tool get_neighbors id=<node id> direction=in`,
`ripplet tool feature_slice feature=<name>`, `ripplet check`.
"""


def block() -> str:
    return f"{BEGIN}\n{RULES.rstrip()}\n{END}\n"


_BLOCK_RX = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
# the same block written under the previous name
_LEGACY_BLOCK_RX = re.compile(re.escape("<!-- atlasgraph:rules:begin -->") + r".*?"
                              + re.escape("<!-- atlasgraph:rules:end -->") + r"\n?", re.S)
LEGACY_NUDGE_MARKS = ("atlasgraph.context.rules",)


def write_block(path: Path) -> str:
    """Insert or replace the rules block. Returns 'added' | 'updated' | 'unchanged'."""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    old = original
    if _LEGACY_BLOCK_RX.search(old):
        # a block written under the previous name takes the current block's
        # place (or goes, when the current block is already there)
        old = _LEGACY_BLOCK_RX.sub("" if BEGIN in old else (lambda _m: block()), old, count=1)
    if BEGIN in old and END in old:
        new = _BLOCK_RX.sub(lambda _m: block(), old, count=1)
        state = "unchanged" if new == original else "updated"
    else:
        sep = "" if not old else ("\n" if old.endswith("\n") else "\n\n")
        new = old + sep + block()
        state = "added"
    if new != original:
        path.write_text(new, encoding="utf-8")
    return state


def remove_block(path: Path) -> bool:
    if not path.exists():
        return False
    old = path.read_text(encoding="utf-8")
    if BEGIN not in old and not _LEGACY_BLOCK_RX.search(old):
        return False
    new = _LEGACY_BLOCK_RX.sub("", _BLOCK_RX.sub("", old, count=1), count=1)
    new = re.sub(r"\n{3,}$", "\n", new)
    if new.strip():
        path.write_text(new, encoding="utf-8")
    else:
        path.unlink()           # the file held only our block: we created it
    return True


def has_block(path: Path) -> bool:
    try:
        t = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return BEGIN in t and END in t


def doc_files(root: Path, target: str) -> list:
    out = []
    if target in ("claude", "both"):
        out.append(root / "CLAUDE.md")
    if target in ("agents", "both"):
        out.append(root / "AGENTS.md")
    return out


# ------------------------------------------------------------ PreToolUse nudge
def nudge_command() -> str:
    return f'"{Path(sys.executable).as_posix()}" -m {NUDGE_MARK}'


def _settings(root: Path) -> Path:
    return root / ".claude" / "settings.json"


def _ours(group: dict) -> bool:
    return any(NUDGE_MARK in (h.get("command") or "") or "ripplet.context.agent_hooks" in (h.get("command") or "") or any(m in (h.get("command") or "") for m in LEGACY_NUDGE_MARKS)
               for h in group.get("hooks", []) if isinstance(h, dict))


def install_nudge(root: Path) -> Path:
    """Claude Code hooks, now the agent-wide guard (context/integrations.py)."""
    from .integrations import install_claude_hooks
    return install_claude_hooks(root, "claude", strict=False)


def _install_nudge_legacy(root: Path) -> Path:
    p = _settings(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        raise SystemExit(f"{p} is not valid JSON; not modified")
    groups = cfg.setdefault("hooks", {}).setdefault("PreToolUse", [])
    groups[:] = [g for g in groups if not (isinstance(g, dict) and _ours(g))]
    groups.append({"matcher": "Grep|Bash",
                   "hooks": [{"type": "command", "command": nudge_command(), "timeout": 5}]})
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def uninstall_nudge(root: Path) -> bool:
    from .integrations import remove_claude_hooks
    return remove_claude_hooks(root, "claude")


def _uninstall_nudge_legacy(root: Path) -> bool:
    p = _settings(root)
    if not p.exists():
        return False
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return False
    groups = (cfg.get("hooks") or {}).get("PreToolUse") or []
    keep = [g for g in groups if not (isinstance(g, dict) and _ours(g))]
    if len(keep) == len(groups):
        return False
    if keep:
        cfg["hooks"]["PreToolUse"] = keep
    else:
        del cfg["hooks"]["PreToolUse"]
        if not cfg["hooks"]:
            del cfg["hooks"]
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return True


def nudge_installed(root: Path) -> bool:
    try:
        cfg = json.loads(_settings(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(isinstance(g, dict) and _ours(g) for g in (cfg.get("hooks") or {}).get("PreToolUse") or [])


_SEARCH_BINS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "findstr", "git-grep"}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_CODE_TYPES = ("class", "function", "method", "interface", "struct", "enum", "trait", "type", "record")
_STOP = {"def", "class", "function", "func", "fn", "public", "private", "static", "return",
         "import", "from", "async", "await", "const", "let", "var", "new", "this", "self",
         "true", "false", "null", "None", "TODO", "FIXME", "interface", "struct", "void",
         "string", "int", "bool", "test", "tests", "src", "include", "type"}


def search_pattern(tool: str, tin: dict) -> str | None:
    """The search pattern of a Grep call or of a grep-like Bash command."""
    if tool == "Grep":
        return str(tin.get("pattern") or "") or None
    if tool != "Bash":
        return None
    cmd = str(tin.get("command") or "")
    for seg in re.split(r"\|\||&&|[|;]", cmd):
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            continue
        if not toks:
            continue
        name = Path(toks[0]).name.lower().removesuffix(".exe")
        args = toks[1:]
        if name == "git" and args[:1] == ["grep"]:
            name, args = "git-grep", args[1:]
        if name not in _SEARCH_BINS:
            continue
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in ("-e", "--regexp"):
                skip_next = False
                continue
            if a.startswith("-"):
                if a in ("-g", "--glob", "-t", "--type", "-A", "-B", "-C", "-m", "--include", "--exclude"):
                    skip_next = True
                continue
            return a
    return None


def _candidates(pattern: str) -> list:
    """Identifiers a regex pattern is clearly looking for (literal parts only)."""
    lit = re.sub(r"\\[bBwWsSdD]", " ", pattern)       # regex escapes are not text
    lit = re.sub(r"\[[^\]]*\]|[(){}|^$*+?.]", " ", lit)
    out = []
    for w in _IDENT.findall(lit):
        if w in _STOP or w.lower() in _STOP or w in out:
            continue
        out.append(w)
    return out[:4]


def _db(root: Path) -> Path | None:
    cfgp = None
    for d in [root, *root.parents]:
        from ..legacy import config_path
        c = config_path(d)
        if c.is_file():
            cfgp = c
            break
    if cfgp is None:
        return None
    try:
        c = json.loads(cfgp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    db = Path(c.get("storage_path") or cfgp.parent / "graph.db")
    if not db.is_absolute():
        db = cfgp.parent.parent / db
    return db if db.is_file() else None


def _lookup(db: Path, names: list) -> list:
    """(name, type, file, line, n_callers) for names that are code symbols."""
    if not names:
        return []
    cx = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1)
    try:
        q = ",".join("?" * len(_CODE_TYPES))
        rows = []
        for n in names:
            r = cx.execute(
                f"SELECT id, label, type, file_path, start_line FROM nodes WHERE label=? AND type IN ({q}) "
                "ORDER BY (file_path LIKE '%test%') LIMIT 3", (n, *_CODE_TYPES)).fetchall()
            if not r or len(r) >= 3:     # absent, or too common a name to point anywhere
                continue
            nid, label, typ, fp, line = r[0]
            callers = cx.execute(
                "SELECT COUNT(DISTINCT source) FROM edges WHERE target=? AND relation IN "
                "('calls','dispatches','instantiates','overrides','implements','inherits','references')",
                (nid,)).fetchone()[0]
            rows.append((label, typ, fp, line, callers, len(r)))
        return rows
    finally:
        cx.close()


def _state_path(db: Path, session: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session or "default")[:80]
    return db.parent / "hook_state" / f"nudge-{safe}.json"


def handle(ev: dict) -> dict | None:
    tool = ev.get("tool_name") or ""
    tin = ev.get("tool_input") or {}
    if not isinstance(tin, dict):
        return None
    pat = search_pattern(tool, tin)
    if not pat:
        return None
    root = Path(ev.get("cwd") or ".")
    db = _db(root)
    if db is None:
        return None
    sp = _state_path(db, str(ev.get("session_id") or ""))
    try:
        st = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {"n": 0, "seen": []}
    if st["n"] >= MAX_NUDGES:
        return None
    names = [n for n in _candidates(pat) if n not in st["seen"]]
    hits = _lookup(db, names)
    if not hits:
        return None
    parts = []
    for label, typ, fp, line, callers, n_defs in hits[:2]:
        where = f"{fp}:{line}" + (f" (+{n_defs - 1} more definitions)" if n_defs > 1 else "")
        parts.append(f"`{label}` is an indexed {typ} defined at {where}, with {callers} known "
                     f"caller/reference site(s)")
    first = hits[0][0]
    msg = ("Ripplet: " + "; ".join(parts) + f". For its definition and callers, "
           f"explain_symbol(symbol='{first}') answers in one call with file:line, including "
           "calls through interfaces, DI and overrides, and without same-named unrelated code. "
           "This search still runs as asked.")
    st["n"] += 1
    st["seen"] += [h[0] for h in hits]
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}}


def main() -> int:
    try:
        ev = json.loads(sys.stdin.buffer.read().decode("utf-8", "ignore") or "{}")
        res = handle(ev) if isinstance(ev, dict) else None
        if res is not None:
            sys.stdout.write(json.dumps(res))
    except Exception:
        pass        # fail open: the agent's tool call always proceeds
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
