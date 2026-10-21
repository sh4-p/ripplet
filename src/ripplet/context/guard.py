"""Change guard: `check_change` run automatically when the agent stops.

The agent never has to remember to call it. When it finishes a turn:

  * Claude Code: a `Stop` hook. If the change has problems the hook answers
    `{"decision": "block", "reason": ...}` and the agent keeps working with
    the findings in front of it; a clean change produces no output at all.
  * OpenCode: a plugin (`.opencode/plugin/ripplet-guard.js`) runs
    `python -m ripplet.context.guard --json <dir>` on `session.idle` and,
    when it blocks, sends the text back into the session as a user message.

Only actionable findings block: callers left on a removed/changed symbol,
calls that resolve to nothing, and places that touch the same data as a
helper this change introduced but do not call it. The message also names
the tests that reach the change with commands to run just those.

Loop guard: one set of findings blocks once. If the agent looks and decides
it is intentional, the next stop goes through. Every decision is logged to
.ripplet/guard.log. Any error lets the agent stop (never traps it).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

MAX_LINES = 18


def find_config(start: Path) -> Path | None:
    from ..legacy import find_config as _fc
    return _fc(start)


def brief(report: dict) -> tuple[bool, str, str]:
    """(should block, message, fingerprint of the blocking findings)."""
    if report.get("error") or not report.get("files"):
        return False, "", ""
    # Only findings proven by the graph and the diff block the agent. Word-
    # similarity guesses (`pattern_gaps`) never do: a weaker model would treat
    # them as orders and "fix" code that was right.
    stale = [s for s in report.get("stale_callers") or [] if s.get("code")]
    dang = [d for d in report.get("dangling_calls") or [] if d.get("code")]
    repl = [r for r in report.get("replacement_gaps") or [] if r.get("code")]
    schema = [x for x in report.get("schema_gaps") or [] if x.get("code")]
    if not (stale or dang or repl or schema):
        return False, "", ""
    short = lambda q: ".".join(q.split(".")[-2:])
    items = []
    for s in stale[:5]:
        items.append(f"{s['at']} still calls {short(s['symbol'])}, which your change "
                     f"{'removed or renamed' if s['why'] == 'removed or renamed' else 'changed (' + s['why'] + ')'}.\n"
                     f"   Code: {s['code']}")
    for r in repl[:5]:
        items.append(f"{r['at']} still calls {short(r['old'])}; your change replaced that call with "
                     f"{short(r['new'])} in {short(r['switched'][0])}.\n   Code: {r['code']}")
    for x in schema[:5]:
        items.append(f"{x['at']} still uses {x['kind']} {x['schema']}, which your migration "
                     f"{x['why'].split(' by ')[0]}.\n   Code: {x['code']}")
    for d in dang[:3]:
        items.append(f"{d['at']} calls {d['call']}, which does not exist in this repo "
                     f"(closest name: {d['did_you_mean']}).\n   Code: {d['code']}")
    total = len(stale) + len(repl) + len(dang) + len(schema)
    head = ("Ripplet found places your change may have missed (from the code graph and your diff)"
            + (f": {total} in all." if total > len(items) else ":"))
    lines = [head]
    lines += [f"{i}. {t}" for i, t in enumerate(items, 1)]
    rest = stale[5:] + repl[5:] + schema[5:] + dang[3:]
    if rest:
        from ..change.check import rest_by_file
        lines.append(f"The same applies to these {len(rest)} other places (file: lines):")
        lines += rest_by_file(rest, limit_files=25)
    tests = report.get("tests") or {}
    if tests.get("commands"):
        lines.append(f"Tests that reach your change: {'; '.join(tests['commands'])[:300]}")
    lines.append("For each item: open the line, update it if it should follow your change; if it should stay "
                 "as it is, leave it. Then finish. This message is shown once.")
    key = json.dumps([[s["caller"], s["symbol"]] for s in stale] + [[d["call"], d["at"]] for d in dang]
                     + [[r["caller"], r["old"]] for r in repl]
                     + [[x["at"], x["schema"]] for x in schema], sort_keys=True)
    return True, "\n".join(lines), hashlib.sha256(key.encode()).hexdigest()[:16]


def evaluate(cwd: Path) -> dict:
    """{block, text, fingerprint, seconds}; never raises."""
    t0 = time.time()
    out = {"block": False, "text": "", "fingerprint": "", "seconds": 0.0}
    try:
        cfgp = find_config(cwd)
        if cfgp is None:
            return out
        from ..config import RippletConfig
        from ..change.check import check_change
        cfg = RippletConfig.load(str(cfgp))
        report = check_change(cfg)
        block, text, fp = brief(report)
        cmds = (report.get("tests") or {}).get("commands") or []
        if cmds and os.environ.get("RIPPLET_GUARD_RUN_TESTS", "") in ("1", "true", "yes"):
            # closed loop (opt-in): run the tests that reach the change; a
            # failure blocks once, with the frame where it broke
            from ..change.verify import run_tests
            from ..storage.sqlite_store import SQLiteStore
            st = SQLiteStore(cfg.storage_path)
            try:
                runs = run_tests(st, cfg, cmds, timeout=float(os.environ.get("RIPPLET_GUARD_TEST_TIMEOUT", "240")))
            finally:
                st.close()
            failed = [r for r in runs if not r["passed"]]
            if failed:
                part = ["Tests that reach your change FAILED. Fix the cause, then finish:"]
                for r in failed[:2]:
                    part.append(f"- {r['command']}")
                    diag = (r.get("diagnosis") or "").strip()
                    body = diag if diag and "No stack frame" not in diag else \
                        "\n".join(r["output_tail"].strip().splitlines()[-20:])
                    part.append("  " + "\n  ".join(body.splitlines()[:30]))
                text = (text + "\n\n" if text else "") + "\n".join(part)
                block = True
                # the same failure blocks once: timings, addresses and counts
                # in the output change between runs and are left out
                stable = lambda t: re.sub(r"\d+(\.\d+)?\s*(s|ms|sec|seconds)\b|0x[0-9a-fA-F]+|\d+", "#", t)
                fp = hashlib.sha256((fp + "|".join(r["command"] + stable(r["output_tail"][-600:])
                                                    for r in failed)).encode()).hexdigest()[:16]
        sdir = cfgp.parent
        seen_p = sdir / "guard_seen.json"
        try:
            seen = set(json.loads(seen_p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            seen = set()
        if block and fp in seen:
            block = False            # already shown once: the agent decided
        if block:
            seen.add(fp)
            seen_p.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        out.update(block=block, text=text if block else "", fingerprint=fp)
        out["seconds"] = round(time.time() - t0, 2)
        _log(sdir, {"t": time.time(), "block": block, "fp": fp, "seconds": out["seconds"],
                    "summary": report.get("summary", "")})
    except Exception as e:  # never trap the agent
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _log(sdir: Path, rec: dict) -> None:
    try:
        with open(sdir / "guard.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--json":
        # OpenCode plugin / scripts: evaluate a directory, print the decision
        res = evaluate(Path(argv[1] if len(argv) > 1 else "."))
        sys.stdout.write(json.dumps(res))
        return 0
    try:
        ev = json.loads(sys.stdin.buffer.read().decode("utf-8", "ignore") or "{}")
    except ValueError:
        return 0
    if ev.get("hook_event_name") not in (None, "Stop", "SubagentStop"):
        return 0
    res = evaluate(Path(ev.get("cwd") or "."))
    if res.get("block"):
        sys.stdout.write(json.dumps({"decision": "block", "reason": res["text"]}))
    return 0


# --------------------------------------------------------------- installation
GUARD_MARK = "ripplet.context.guard"
PLUGIN_NAME = "ripplet-guard.js"
# installs made under the previous name, removed whenever ours are (re)written
LEGACY_MARKS = ("atlasgraph.context.guard",)
LEGACY_PLUGINS = ("atlasgraph-guard.js",)


def _is_guard(cmd: str) -> bool:
    return GUARD_MARK in cmd or any(m in cmd for m in LEGACY_MARKS)


def guard_command() -> str:
    return f'"{Path(sys.executable).as_posix()}" -m {GUARD_MARK}'


def install_claude(root: Path) -> Path:
    p = root / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        raise SystemExit(f"{p} is not valid JSON; not modified")
    groups = cfg.setdefault("hooks", {}).setdefault("Stop", [])
    groups[:] = [g for g in groups if not any(_is_guard(h.get("command") or "") for h in g.get("hooks", []))]
    groups.append({"hooks": [{"type": "command", "command": guard_command(), "timeout": 120}]})
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def uninstall_claude(root: Path) -> bool:
    p = root / ".claude" / "settings.json"
    if not p.exists():
        return False
    cfg = json.loads(p.read_text(encoding="utf-8"))
    groups = (cfg.get("hooks") or {}).get("Stop") or []
    keep = [g for g in groups if not any(_is_guard(h.get("command") or "") for h in g.get("hooks", []))]
    if len(keep) == len(groups):
        return False
    if keep:
        cfg["hooks"]["Stop"] = keep
    else:
        del cfg["hooks"]["Stop"]
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return True


_PLUGIN = r"""// Ripplet change guard for OpenCode (generated by `ripplet guard on`).
// When a session goes idle, check the change; if something is missing, say so
// in the session so the agent continues. Clean changes produce nothing.
import { spawnSync } from "node:child_process";

const PYTHON = __PYTHON__;
const busy = new Set();

export const RippletGuard = async ({ client, directory }) => ({
  event: async ({ event }) => {
    if (event.type !== "session.idle") return;
    const sid = event.properties?.sessionID;
    if (!sid || busy.has(sid)) return;
    busy.add(sid);
    try {
      const r = spawnSync(PYTHON, ["-m", "ripplet.context.guard", "--json", directory],
        { encoding: "utf8", timeout: 170000, cwd: directory, env: { ...process.env, PYTHONPATH: __PYTHONPATH__ } });
      let res = {};
      try { res = JSON.parse(r.stdout || "{}"); } catch { res = {}; }
      if (res.block && res.text) {
        await client.session.prompt({ path: { id: sid }, body: { parts: [{ type: "text", text: res.text }] } });
      }
    } catch (e) {
      // never break the session
    } finally {
      busy.delete(sid);
    }
  },
});
"""


def install_opencode(root: Path, pythonpath: str = "") -> Path:
    p = root / ".opencode" / "plugin" / PLUGIN_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    src = _PLUGIN.replace("__PYTHON__", json.dumps(Path(sys.executable).as_posix())) \
                 .replace("__PYTHONPATH__", json.dumps(pythonpath or ""))
    p.write_text(src, encoding="utf-8")
    for old in LEGACY_PLUGINS:
        (p.parent / old).unlink(missing_ok=True)
    return p


def uninstall_opencode(root: Path) -> bool:
    removed = False
    for name in (PLUGIN_NAME,) + LEGACY_PLUGINS:
        p = root / ".opencode" / "plugin" / name
        if p.exists():
            p.unlink()
            removed = True
    return removed


def history(root: Path, limit: int = 20) -> list:
    cfgp = find_config(root)
    if cfgp is None:
        return []
    try:
        lines = (cfgp.parent / "guard.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(x) for x in lines[-limit:] if x.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
