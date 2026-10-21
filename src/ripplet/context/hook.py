"""Claude Code PostToolUse hook: compact Read and Grep results.

Reads the hook event on stdin and, when a shaped result is clearly smaller,
prints `updatedToolOutput` in the tool's own result shape. Any error or
non-applicable case prints nothing (Claude Code keeps the original output),
so the hook can never break a session.

Per-session state (.ripplet/hook_state/<session>.json) remembers which
files were already shown compactly — a second plain Read of the same file
returns it whole — and the line numbers of earlier grep hits, which steer
which parts of a later Read are shown in full. Every intervention is logged
to .ripplet/hook.log (`ripplet hooks stats` sums it).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .fileview import build_view, find_index, task_terms
from .grepview import shape_grep

_MAX_TRANSCRIPT_BYTES = 400_000


def _state_dir(start: Path) -> Path | None:
    loc = find_index(start)
    if loc is None:
        return None
    return Path(loc[0]).parent


def _load_state(sdir: Path, session: str) -> dict:
    try:
        return json.loads((sdir / "hook_state" / f"{session}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"viewed": [], "grep_hits": {}, "patterns": []}


def _save_state(sdir: Path, session: str, st: dict) -> None:
    d = sdir / "hook_state"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.json").write_text(json.dumps(st), encoding="utf-8")


def _log(sdir: Path, rec: dict) -> None:
    try:
        with open(sdir / "hook.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _prompt_text(transcript: str) -> str:
    """User prompts of this session (tail of the transcript), not tool output."""
    try:
        p = Path(transcript)
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > _MAX_TRANSCRIPT_BYTES:
                fh.seek(size - _MAX_TRANSCRIPT_BYTES)
            raw = fh.read().decode("utf-8", "ignore")
    except OSError:
        return ""
    out = []
    for line in raw.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "user":
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out += [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(out)


def handle(ev: dict) -> dict | None:
    if ev.get("hook_event_name") != "PostToolUse":
        return None
    tool, ti, resp = ev.get("tool_name"), ev.get("tool_input") or {}, ev.get("tool_response")
    session = str(ev.get("session_id") or "nosession")
    cwd = ev.get("cwd") or os.getcwd()
    if not isinstance(resp, dict):
        return None
    if tool == "Read":
        f = resp.get("file") or {}
        if resp.get("type") != "text" or not isinstance(f.get("content"), str):
            return None
        path = Path(ti.get("file_path") or f.get("filePath") or "")
        sdir = _state_dir(path)
        if sdir is None:
            return None
        st = _load_state(sdir, session)
        key = str(path.resolve())
        if key in st["viewed"]:
            return None   # second plain read: the agent wants the whole file
        terms = task_terms(_prompt_text(ev.get("transcript_path") or "")) + st.get("patterns", [])
        view = build_view(str(path), terms=terms, hit_lines=set(st["grep_hits"].get(key, [])),
                          offset=ti.get("offset"), limit=ti.get("limit"))
        if view is None:
            return None
        st["viewed"].append(key)
        _save_state(sdir, session, st)
        _log(sdir, {"ts": time.time(), "tool": "Read", "file": key, "orig": len(f["content"]),
                    "new": len(view.content), "session": session})
        nf = dict(f, content=view.content, numLines=view.content.count("\n") + 1, startLine=1)
        out = {"type": "text", "file": nf}
    elif tool == "Grep":
        content = resp.get("content")
        if not isinstance(content, str):
            return None
        sdir = _state_dir(Path(cwd) / "_")
        shaped = shape_grep(ti, content, cwd)
        if sdir is None or shaped is None:
            return None
        new, hits = shaped
        st = _load_state(sdir, session)
        for k, lines in hits.items():
            k2 = str(Path(k).resolve())
            st["grep_hits"][k2] = sorted(set(st["grep_hits"].get(k2, [])) | set(lines))[:500]
        pat = str(ti.get("pattern") or "")
        st["patterns"] = (st.get("patterns", []) + task_terms(pat))[-30:]
        _save_state(sdir, session, st)
        _log(sdir, {"ts": time.time(), "tool": "Grep", "pattern": pat[:120], "orig": len(content),
                    "new": len(new), "session": session})
        out = dict(resp, content=new, numLines=new.count("\n") + 1)
    else:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": out}}


def main() -> int:
    try:
        ev = json.loads(sys.stdin.buffer.read().decode("utf-8", "ignore") or "{}")
        res = handle(ev)
        if res is not None:
            sys.stdout.write(json.dumps(res))
    except Exception:
        pass   # never break the agent's tool call
    return 0


# --------------------------------------------------------------- installation
HOOK_MARK = "ripplet.context.hook"
LEGACY_HOOK_MARKS = ("atlasgraph.context.hook",)


def _is_hook(cmd: str) -> bool:
    return HOOK_MARK in cmd or any(m in cmd for m in LEGACY_HOOK_MARKS)


def hook_command() -> str:
    # Quoted, forward slashes: Claude Code may run hooks through bash even on
    # Windows, where an unquoted C:\... path loses its backslashes.
    return f'"{Path(sys.executable).as_posix()}" -m {HOOK_MARK}'


def install(root: Path) -> Path:
    p = root / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        raise SystemExit(f"{p} is not valid JSON; not modified")
    groups = cfg.setdefault("hooks", {}).setdefault("PostToolUse", [])
    groups[:] = [g for g in groups if not any(_is_hook(h.get("command") or "")
                                              for h in g.get("hooks", []))]
    groups.append({"matcher": "Read|Grep",
                   "hooks": [{"type": "command", "command": hook_command(), "timeout": 5}]})
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def uninstall(root: Path) -> bool:
    p = root / ".claude" / "settings.json"
    if not p.exists():
        return False
    cfg = json.loads(p.read_text(encoding="utf-8"))
    groups = (cfg.get("hooks") or {}).get("PostToolUse") or []
    keep = [g for g in groups if not any(_is_hook(h.get("command") or "") for h in g.get("hooks", []))]
    if len(keep) == len(groups):
        return False
    cfg["hooks"]["PostToolUse"] = keep
    if not keep:
        del cfg["hooks"]["PostToolUse"]
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return True


def stats(root: Path) -> dict:
    sdir = _state_dir(root / "_")
    out = {"Read": [0, 0, 0], "Grep": [0, 0, 0]}   # interventions, original chars, shown chars
    if sdir is None:
        return out
    try:
        for line in (sdir / "hook.log").read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            o = out.setdefault(r["tool"], [0, 0, 0])
            o[0] += 1
            o[1] += r["orig"]
            o[2] += r["new"]
    except (OSError, ValueError):
        pass
    return out


if __name__ == "__main__":
    raise SystemExit(main())
