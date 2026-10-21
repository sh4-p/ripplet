"""Close the loop after an edit: run exactly the tests that reach the change
and explain the failures.

`check_change` already knows which tests reach a change and the commands
that run only those. This runs them (opt-in: the CLI flag `--run-tests`,
or RIPPLET_ALLOW_TEST_RUN=1 for the MCP tool and the guard) and, when a
command fails, maps its output to the code with investigate_error: the
failing test, the repo frame where it broke, the code there.

Commands run with shell=False in the repository root, one at a time, with
a timeout; output is kept to its tail. Nothing runs without the opt-in.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time

TAIL = 6000
ALLOW_ENV = "RIPPLET_ALLOW_TEST_RUN"


def allowed() -> bool:
    return os.environ.get(ALLOW_ENV, "") in ("1", "true", "yes")


def _argv(cmd: str) -> list:
    argv = shlex.split(cmd, posix=True)
    if argv and argv[0] in ("pytest", "py.test"):
        import sys
        argv = [sys.executable, "-m", "pytest"] + argv[1:]
    return argv


def run_tests(store, cfg, commands: list, timeout: float = 300.0, max_commands: int = 3) -> list:
    from ..retrieval.error_trace import investigate_error
    root = str((getattr(cfg, "workspace_roots", None) or ["."])[0])
    out = []
    for cmd in commands[:max_commands]:
        t0 = time.time()
        rec = {"command": cmd}
        try:
            p = subprocess.run(_argv(cmd), cwd=root, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
            rec["returncode"] = p.returncode
            text = (p.stdout or "") + "\n" + (p.stderr or "")
        except subprocess.TimeoutExpired as e:
            rec["returncode"] = None
            text = f"timed out after {timeout:.0f}s\n" + str(e.stdout or "")[-TAIL:]
        except (OSError, ValueError) as e:
            rec["returncode"] = None
            text = f"could not start: {e}"
        rec["seconds"] = round(time.time() - t0, 1)
        rec["passed"] = rec["returncode"] == 0
        rec["output_tail"] = text[-TAIL:]
        if not rec["passed"] and rec["returncode"] is not None:
            try:
                rec["diagnosis"] = investigate_error(store, cfg, text[-60000:])
            except Exception as e:     # diagnosis is extra: never lose the run
                rec["diagnosis"] = f"(diagnosis failed: {type(e).__name__})"
        out.append(rec)
    return out


def render_runs(runs: list) -> str:
    if not runs:
        return ""
    lines = ["\n## Tests run for this change"]
    for r in runs:
        status = "PASSED" if r["passed"] else ("ERROR" if r["returncode"] is None else "FAILED")
        lines.append(f"- {status} ({r['seconds']}s): {r['command']}")
        if not r["passed"]:
            diag = r.get("diagnosis") or ""
            if diag and "No stack frame" not in diag:
                lines.append("  " + diag.replace("\n", "\n  "))
            else:
                tail = "\n".join(r["output_tail"].strip().splitlines()[-25:])
                lines.append("  last output:\n  " + tail.replace("\n", "\n  "))
    return "\n".join(lines)
