"""Bounded subprocess execution (Phase 10 Stage 5).

Single entry point `run_bounded`: fixed argv list, `shell=False` always,
working directory = validated repo root, wall-clock timeout, output-byte cap,
platform-appropriate termination (Windows process tree via taskkill +
TerminateProcess; POSIX process groups). The child environment is inherited
but never recorded. Returns facts only; outcome mapping lives in service.py.
"""
from __future__ import annotations
import os
import subprocess
import sys
import threading
import time

CHUNK = 65536


def _popen_kwargs():
    kw: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                "text": True, "encoding": "utf-8", "errors": "replace",
                "shell": False}
    if sys.platform == "win32":
        try:
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        except AttributeError:
            pass
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kw["startupinfo"] = si
        except (AttributeError, OSError):
            pass
    else:
        try:
            import functools
            kw["preexec_fn"] = lambda: os.setsid()
        except (AttributeError, OSError):
            pass
    return kw


def terminate_tree(proc: subprocess.Popen) -> list[str]:
    """Best-effort full-tree termination. Returns steps attempted."""
    steps: list[str] = []
    if proc.poll() is not None:
        return ["already-exited"]
    if sys.platform == "win32":
        # Kill the whole tree first (pytest spawns no children normally, but
        # fixtures/plugins may); fall back to direct kill; always reap.
        try:
            r = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            steps.append(f"taskkill-t:{r.returncode}")
        except Exception as e:
            steps.append(f"taskkill-failed:{type(e).__name__}")
        try:
            proc.kill()
            steps.append("kill")
        except Exception as e:
            steps.append(f"kill-failed:{type(e).__name__}")
    else:
        try:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            steps.append("killpg-term")
        except Exception as e:
            steps.append(f"killpg-failed:{type(e).__name__}")
        try:
            proc.terminate()
            steps.append("terminate")
        except Exception as e:
            steps.append(f"terminate-failed:{type(e).__name__}")
    try:
        proc.wait(timeout=15)
        steps.append("reaped")
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
            steps.append("reaped-after-kill")
        except Exception as e:
            steps.append(f"unreaped:{type(e).__name__}")
    return steps


def run_bounded(argv: list[str], cwd: str, timeout_s: float,
                max_output_bytes: int) -> dict:
    """Run argv to completion under bounds. Never raises for process outcomes;
    raises only for spawn failures (OSError) so callers record runner_error."""
    if not argv or not isinstance(argv, list) or \
            any(not isinstance(a, str) for a in argv):
        raise ValueError("argv must be a list of strings")
    t0 = time.time()
    proc = subprocess.Popen(argv, cwd=cwd, **_popen_kwargs())
    out_parts: list[str] = []
    out_bytes = 0
    truncated = False
    limit_hit = False
    done = threading.Event()

    def _pump():
        nonlocal out_bytes, truncated, limit_hit
        try:
            while True:
                chunk = proc.stdout.read(CHUNK) if proc.stdout else ""
                if not chunk:
                    break
                b = len(chunk.encode("utf-8", "replace"))
                if out_bytes + b > max_output_bytes:
                    # keep a capped prefix; the run is terminated by the caller
                    room = max_output_bytes - out_bytes
                    if room > 0:
                        out_parts.append(chunk.encode("utf-8", "replace")[:room].decode(
                            "utf-8", "replace"))
                    out_bytes = max_output_bytes
                    truncated = True
                    limit_hit = True
                    break
                out_parts.append(chunk)
                out_bytes += b
        finally:
            done.set()

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    deadline = t0 + max(float(timeout_s), 0.1)
    timed_out = False
    term_steps: list[str] = []
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        if limit_hit:
            term_steps = terminate_tree(proc)
            break
        if time.time() >= deadline:
            timed_out = True
            term_steps = terminate_tree(proc)
            break
        done.wait(timeout=0.05)
    done.wait(timeout=10)
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass
    rc = proc.poll()
    if rc is None:
        # termination failed to reap; last-resort kill (recorded, not silent)
        try:
            proc.kill()
            rc = proc.wait(timeout=15)
            term_steps.append("last-resort-kill")
        except Exception as e:
            term_steps.append(f"reap-failed:{type(e).__name__}")
    text = "".join(out_parts)
    return {"returncode": rc, "output": text, "output_bytes": out_bytes,
            "truncated": truncated or limit_hit, "limit_hit": limit_hit,
            "timed_out": timed_out, "term_steps": term_steps,
            "duration_ms": round((time.time() - t0) * 1000, 1),
            "pid": proc.pid}
