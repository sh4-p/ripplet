"""Install Ripplet's guidance into every coding agent a repository may be used with.

Two layers, both reversible (`ripplet rules off`):

  rules    the always-on text (rules.RULES) in the file each agent always reads:
           CLAUDE.md (Claude Code), AGENTS.md (OpenCode, Codex, Hermes, Amp, Devin,
           Jules...), GEMINI.md (Gemini CLI), .cursor/rules/ripplet.mdc (Cursor),
           .github/copilot-instructions.md (Copilot), .kiro/steering/ripplet.md
           (Kiro), .windsurf/rules/ripplet.md (Windsurf/Devin), .agents/rules/
           ripplet.md (Antigravity), .clinerules/ripplet.md (Cline), and a Claude Code
           skill (.claude/skills/ripplet/SKILL.md, loaded when a task matches it)
  hooks    agents that let a tool call be intercepted: Claude Code and Codebuddy
           (PreToolUse + PostToolUse), OpenCode (a tool.execute.before plugin),
           Gemini CLI (BeforeTool). Codex rejects added context on PreToolUse, so it
           gets AGENTS.md only. Hooks remind once per session; `strict` blocks the
           first raw read/search of source once, until a Ripplet tool is used.

`auto` writes CLAUDE.md + AGENTS.md + the Claude Code skill always, and every other agent's files when
the repository already has that agent's folder or file.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import rules as _rules

GUARD = "ripplet.context.agent_hooks"
OWN = "ripplet"               # marker in hook commands / plugin ids

# (target, path, kind): "block" = marked block inside a shared file,
# "file" = a file Ripplet owns, with the given front matter
TEXT_TARGETS = {
    "claude": ("CLAUDE.md", "block", ""),
    "agents": ("AGENTS.md", "block", ""),
    "gemini": ("GEMINI.md", "block", ""),
    "copilot": (".github/copilot-instructions.md", "block", ""),
    "cursor": (".cursor/rules/ripplet.mdc", "file",
               "---\ndescription: Ripplet code graph of this repository\nalwaysApply: true\n---\n\n"),
    "kiro": (".kiro/steering/ripplet.md", "file", "---\ninclusion: always\n---\n\n"),
    "windsurf": (".windsurf/rules/ripplet.md", "file", "---\ntrigger: always_on\n---\n\n"),
    "antigravity": (".agents/rules/ripplet.md", "file", ""),
    "cline": (".clinerules/ripplet.md", "file", ""),
    # Claude Code skill: loaded on demand when a task matches its description
    "skill": (".claude/skills/ripplet/SKILL.md", "file", None),
}
SKILL_FRONT = (
    "---\n"
    "name: ripplet\n"
    "description: Use the Ripplet code graph of this repository instead of grep/read for code "
    "structure - who calls a function, what breaks if it changes, renaming or changing a "
    "signature, starting a task, tracing an error, routes and their permission checks, "
    "database tables and their queries, which tests to run after an edit.\n"
    "---\n\n")


def skill_text() -> str:
    """The SKILL.md body: the same guidance as the always-on rules."""
    return SKILL_FRONT + _rules.block() + "\n"
HOOK_TARGETS = ("claude", "codebuddy", "opencode", "gemini")
# a repository "uses" an agent when one of these exists
DETECT = {
    "gemini": ("GEMINI.md", ".gemini"), "copilot": (".github/copilot-instructions.md", ".github/instructions"),
    "cursor": (".cursor", ".cursorrules"), "kiro": (".kiro",), "windsurf": (".windsurf", ".windsurfrules"),
    "antigravity": (".agents",), "cline": (".clinerules",), "codebuddy": (".codebuddy", "CODEBUDDY.md"),
    "opencode": ("opencode.json", ".opencode", "opencode.jsonc"),
}
ALL = sorted(set(TEXT_TARGETS) | set(HOOK_TARGETS))


def resolve_targets(root: Path, target: str) -> list:
    if target == "all":
        return ALL
    if target == "both":                       # historical: CLAUDE.md + AGENTS.md (+ Claude hook)
        return ["claude", "agents"]
    if target in ("auto", ""):
        out = ["claude", "agents", "skill"]
        for t, marks in DETECT.items():
            if any((root / m).exists() for m in marks):
                out.append(t)
        return sorted(set(out))
    return [t.strip() for t in target.split(",") if t.strip()]


# ------------------------------------------------------------------ rules text
def _text(root: Path, t: str) -> Path:
    return root / TEXT_TARGETS[t][0]


def write_text(root: Path, t: str) -> str:
    rel, kind, front = TEXT_TARGETS[t]
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if kind == "block":
        return _rules.write_block(p)
    body = skill_text() if t == "skill" else front + _rules.block() + "\n"
    old = p.read_text(encoding="utf-8") if p.exists() else None
    if old == body:
        return "unchanged"
    p.write_text(body, encoding="utf-8")
    return "updated" if old is not None else "added"


def remove_text(root: Path, t: str) -> bool:
    rel, kind, _front = TEXT_TARGETS[t]
    p = root / rel
    if kind == "block":
        return _rules.remove_block(p)
    if p.exists() and _rules.BEGIN in p.read_text(encoding="utf-8", errors="ignore"):
        p.unlink()
        if t == "skill":
            try:
                p.parent.rmdir()            # .claude/skills/ripplet, when now empty
            except OSError:
                pass
        return True
    return False


def text_on(root: Path, t: str) -> bool:
    p = _text(root, t)
    return p.exists() and _rules.BEGIN in p.read_text(encoding="utf-8", errors="ignore")


# ------------------------------------------------------- Claude Code / Codebuddy
def _guard_cmd(kind: str, strict: bool = False) -> str:
    return f'"{Path(sys.executable).as_posix()}" -m {GUARD} {kind}' + (" --strict" if strict else "")


def _settings_path(root: Path, t: str) -> Path:
    return root / (".claude" if t == "claude" else ".codebuddy") / "settings.json"


def _is_ours(group) -> bool:
    return isinstance(group, dict) and any(
        isinstance(h, dict) and (GUARD in (h.get("command") or "") or _rules.NUDGE_MARK in (h.get("command") or "")
                                 or any(m in (h.get("command") or "") for m in _rules.LEGACY_NUDGE_MARKS))
        for h in group.get("hooks", []))


def _load_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        raise SystemExit(f"{p} is not valid JSON; not modified")
    if not isinstance(d, dict):
        raise SystemExit(f"{p} is not a JSON object; not modified")
    return d


def install_claude_hooks(root: Path, t: str, strict: bool) -> Path:
    p = _settings_path(root, t)
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = _load_json(p)
    hooks = cfg.setdefault("hooks", {})
    pre = [g for g in hooks.get("PreToolUse", []) if not _is_ours(g)]
    pre.append({"matcher": "Read|Grep|Glob|Bash",
                "hooks": [{"type": "command", "command": _guard_cmd("claude", strict), "timeout": 5}]})
    post = [g for g in hooks.get("PostToolUse", []) if not _is_ours(g)]
    post.append({"matcher": "mcp__ripplet__.*",
                 "hooks": [{"type": "command", "command": _guard_cmd("claude-post"), "timeout": 5}]})
    hooks["PreToolUse"], hooks["PostToolUse"] = pre, post
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def remove_claude_hooks(root: Path, t: str) -> bool:
    p = _settings_path(root, t)
    if not p.exists():
        return False
    cfg = _load_json(p)
    hooks = cfg.get("hooks") or {}
    changed = False
    for ev in ("PreToolUse", "PostToolUse"):
        groups = hooks.get(ev) or []
        keep = [g for g in groups if not _is_ours(g)]
        if len(keep) != len(groups):
            changed = True
            if keep:
                hooks[ev] = keep
            else:
                hooks.pop(ev, None)
    if changed:
        if not hooks:
            cfg.pop("hooks", None)
        p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return changed


# --------------------------------------------------------------------- Gemini CLI
def install_gemini_hook(root: Path) -> Path:
    p = root / ".gemini" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = _load_json(p)
    hooks = cfg.setdefault("hooks", {})
    bt = [g for g in hooks.get("BeforeTool", []) if not _is_ours(g)]
    bt.append({"matcher": "read_file|read_many_files|search_file_content|glob|run_shell_command",
               "hooks": [{"type": "command", "command": _guard_cmd("gemini")}]})
    hooks["BeforeTool"] = bt
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def remove_gemini_hook(root: Path) -> bool:
    p = root / ".gemini" / "settings.json"
    if not p.exists():
        return False
    cfg = _load_json(p)
    groups = (cfg.get("hooks") or {}).get("BeforeTool") or []
    keep = [g for g in groups if not _is_ours(g)]
    if len(keep) == len(groups):
        return False
    if keep:
        cfg["hooks"]["BeforeTool"] = keep
    else:
        cfg["hooks"].pop("BeforeTool", None)
        if not cfg["hooks"]:
            cfg.pop("hooks")
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return True


# ----------------------------------------------------------------------- OpenCode
OPENCODE_PLUGIN = Path(".opencode") / "plugins" / "ripplet.js"
# OpenCode versions differ on the folder (`plugin/` in older docs, `plugins/`
# now). The file goes into both; state is shared through globalThis, so two
# loaded copies still remind and block once, and each id is unique.
OPENCODE_PLUGIN_DIRS = (Path(".opencode") / "plugins", Path(".opencode") / "plugin")
_PLUGIN_JS = r"""// ripplet OpenCode plugin (written by `ripplet rules on`; remove with `ripplet rules off`).
// Steers code-structure lookups to the Ripplet graph tools: one reminder per
// session before the first shell command; with STRICT, raw grep/glob/read of
// source code is blocked until a ripplet_* tool has run (at most 3 times, so
// the model is never stranded; the error names the tool to use).
// OpenCode 1 reads `server()`, OpenCode 2 reads `id` + `setup()`.
import { existsSync, mkdirSync, writeFileSync } from "fs";
import { join } from "path";

const STRICT = __STRICT__;
const REMINDER = __REMINDER__;
const DENY = __DENY__;
const SOURCE = /\.(py|jsx?|tsx?|mjs|cjs|java|kts?|go|rs|cs|php|rb|lua|swift|dart|c|h|cc|cpp|hpp|scala|vue|svelte|sql)$/i;
const SHELL_SEARCH = /(^|[;&|(]\s*)(grep|egrep|rg|ag|ack|findstr|git\s+grep|select-string|sls)\b/i;
// keep the echoed text free of quotes, backticks and $ (it runs in the user's shell)
const quote = (s) => 'echo "' + s.replace(/["`$\\]/g, "") + '"';
const ECHO = quote(REMINDER) + " ; ";
const STOP = __STOP__;
// the searched word, so the note can name the exact call to make next
function searched(cmd) {
  const m = cmd.match(/(?:grep|egrep|rg|ag|ack|findstr|git\s+grep|select-string|sls)\b(?:\s+-{1,2}[\w=-]+)*\s+(?:-Pattern\s+)?["']?([A-Za-z_][\w.:]*)/i);
  return m ? m[1].split(/[.:]+/).pop() : "";
}
function stopNote(cmd) {
  const w = searched(cmd);
  const lead = w ? "Ripplet: this search was not run. Next, call the tool ripplet_explain_symbol with symbol=" + w +
    " (its source, every caller and callee). To rename it: ripplet_rename_symbol. To change its parameters: ripplet_change_plan. "
    : "Ripplet: this search was not run. ";
  return quote(lead + STOP);
}
const state = (globalThis.__rippletGuard ??= new Map());
const ID = "ripplet-guard-" + [...import.meta.url].reduce((h, c) => (Math.imul(h, 31) + c.charCodeAt(0)) >>> 0, 7).toString(16);

// which folder and which plugin API OpenCode actually loaded (`ripplet rules status`)
function stamp(dir, api) {
  try {
    const d = join(dir, ".ripplet", "hook_state");
    mkdirSync(d, { recursive: true });
    writeFileSync(join(d, "opencode-plugin.json"),
      JSON.stringify({ api, file: import.meta.url, at: new Date().toISOString() }));
  } catch (e) { /* diagnostics only */ }
}

function decide(key, tool, args, dir) {
  if (!existsSync(join(dir, ".ripplet", "graph.db"))) return null;
  let st = state.get(key);
  if (!st) { st = {}; state.set(key, st); }
  if (tool.startsWith("ripplet_")) { st.used = true; return null; }
  if (st.used) return null;
  const target = String(args?.filePath ?? args?.path ?? args?.pattern ?? args?.include ?? "");
  const source = tool === "grep" || SOURCE.test(target);
  if (STRICT && (st.denies ?? 0) < 3 && source && (tool === "read" || tool === "grep" || tool === "glob")) {
    st.denies = (st.denies ?? 0) + 1;
    return "deny";
  }
  // a code search run through the shell: an echoed reminder arrives after the
  // search already ran, so stop it instead (once by default, up to 3 if STRICT)
  const shell = (tool === "bash" || tool === "shell") && typeof args?.command === "string";
  if (shell && SHELL_SEARCH.test(args.command) && (st.denies ?? 0) < (STRICT ? 3 : 1)) {
    st.denies = (st.denies ?? 0) + 1;
    st.reminded = true;
    return "stop";
  }
  if ((tool === "bash" || tool === "shell") && !st.reminded && typeof args?.command === "string") {
    st.reminded = true;
    return "remind";
  }
  return null;
}

export default {
  id: ID,

  async setup(ctx) {
    stamp(ctx.location?.directory ?? process.cwd(), "setup (OpenCode 2)");
    await ctx.tool.hook("execute.before", (event) => {
      const dir = ctx.location?.directory ?? process.cwd();
      const d = decide(String(event.sessionID ?? dir), String(event.tool ?? ""), event.input, dir);
      if (d === "remind") event.input = { ...event.input, command: ECHO + event.input.command };
      if (d === "stop") event.input = { ...event.input, command: stopNote(event.input.command) };
      if (d === "deny") throw new Error(DENY);
    });
  },

  async server({ directory }) {
    stamp(directory, "server (OpenCode 1)");
    return {
      "tool.execute.before": async (input, output) => {
        const d = decide(String(input.sessionID ?? directory), String(input.tool ?? ""), output.args, directory);
        if (d === "remind") output.args.command = ECHO + output.args.command;
        if (d === "stop") output.args.command = stopNote(output.args.command);
        if (d === "deny") throw new Error(DENY);
      },
    };
  },
};
"""


def install_opencode_plugin(root: Path, strict: bool) -> Path:
    import re
    from .agent_hooks import DENY, LOAD, REMINDER
    # OpenCode names MCP tools <server>_<tool>: ripplet_task_context, ...
    names = re.compile(r"(?<!ripplet_)\b(task_context|explain_symbol|get_neighbors|analyze_change_impact|"
                       r"list_routes|rename_symbol|change_plan|feature_slice|check_change)\b")
    deny = names.sub(r"ripplet_\1", DENY.replace(" " + LOAD, "")).replace(
        "Ripplet (strict)", "Ripplet (strict, OpenCode)")
    # a shell search is not failed but replaced by this note, so the run goes on
    stop = ("Other tools: ripplet_task_context (start of a task), ripplet_get_neighbors (who calls X), "
            "ripplet_list_routes (endpoints and access checks), ripplet_check_change (after editing). "
            "Rerun the search only for plain text (strings, config keys, messages)."
            + ("" if not strict else " Shell searches of the code are skipped until a ripplet_* tool has run."))
    js = (_PLUGIN_JS.replace("__STRICT__", "true" if strict else "false")
          .replace("__REMINDER__", json.dumps(REMINDER)).replace("__DENY__", json.dumps(deny))
          .replace("__STOP__", json.dumps(stop)))
    for d in OPENCODE_PLUGIN_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "ripplet.js").write_text(js, encoding="utf-8")
    return root / OPENCODE_PLUGIN


def remove_opencode_plugin(root: Path) -> bool:
    removed = False
    for d in OPENCODE_PLUGIN_DIRS:
        p = root / d / "ripplet.js"
        if p.exists() and "ripplet OpenCode plugin" in p.read_text(encoding="utf-8", errors="ignore"):
            p.unlink()
            removed = True
    return removed


# ------------------------------------------------------------------- entry points
def on(root: Path, target: str = "auto", strict: bool = False, hooks: bool = True) -> list:
    """Write every chosen agent's rules (and hooks); returns report lines."""
    out = []
    for t in resolve_targets(root, target):
        if t in TEXT_TARGETS:
            kind = "rules block" if TEXT_TARGETS[t][1] == "block" else "rules"
            out.append(f"{TEXT_TARGETS[t][0]}: {kind} {write_text(root, t)}")
        if not hooks:
            continue
        if t in ("claude", "codebuddy"):
            out.append(f"{_settings_path(root, t).relative_to(root).as_posix()}: PreToolUse/PostToolUse hooks"
                       + (" (strict)" if strict else ""))
            install_claude_hooks(root, t, strict)
        elif t == "opencode":
            install_opencode_plugin(root, strict)
            out.append(f"{OPENCODE_PLUGIN.as_posix()}: tool.execute.before plugin" + (" (strict)" if strict else ""))
        elif t == "gemini":
            install_gemini_hook(root)
            out.append(".gemini/settings.json: BeforeTool hook")
    if "agents" in resolve_targets(root, target):
        out.append("Codex, Hermes, Amp, Devin and other AGENTS.md readers: AGENTS.md (Codex hooks cannot add context)")
    return out


def off(root: Path) -> list:
    out = []
    for t in TEXT_TARGETS:
        if remove_text(root, t):
            out.append(f"{TEXT_TARGETS[t][0]}: rules removed")
    for t in ("claude", "codebuddy"):
        if remove_claude_hooks(root, t):
            out.append(f"{_settings_path(root, t).relative_to(root).as_posix()}: hooks removed")
    if remove_opencode_plugin(root):
        out.append(f"{OPENCODE_PLUGIN.as_posix()}: removed")
    if remove_gemini_hook(root):
        out.append(".gemini/settings.json: hook removed")
    return out


def status(root: Path) -> list:
    out = [f"{TEXT_TARGETS[t][0]:40} {'on' if text_on(root, t) else 'off'}" for t in TEXT_TARGETS]
    for t in ("claude", "codebuddy"):
        p = _settings_path(root, t)
        on_ = p.exists() and GUARD in p.read_text(encoding="utf-8", errors="ignore")
        strict = on_ and "--strict" in p.read_text(encoding="utf-8", errors="ignore")
        out.append(f"{p.relative_to(root).as_posix():40} {'on' + (' (strict)' if strict else '') if on_ else 'off'}")
    pj = root / OPENCODE_PLUGIN
    out.append(f"{OPENCODE_PLUGIN.as_posix():40} "
               f"{('on' + (' (strict)' if 'const STRICT = true' in pj.read_text(encoding='utf-8') else '')) if pj.exists() else 'off'}")
    try:
        ld = json.loads((root / ".ripplet" / "hook_state" / "opencode-plugin.json").read_text(encoding="utf-8"))
        out.append(f"{'  OpenCode loaded it':40} {ld.get('at', '')[:19]} via {ld.get('api')} from "
                   f"{str(ld.get('file', '')).rsplit('/.opencode/', 1)[-1]}")
    except (OSError, ValueError):
        if pj.exists():
            out.append(f"{'  OpenCode loaded it':40} not yet (restart OpenCode in this folder)")
    g = root / ".gemini" / "settings.json"
    out.append(f"{'.gemini/settings.json':40} {'on' if g.exists() and GUARD in g.read_text(encoding='utf-8') else 'off'}")
    return out
