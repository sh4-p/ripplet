"""Runner adapters (Phase 10 Stage 3).

`TestRunnerAdapter`: validate ids -> argv (list, shell=False) -> parse.
Only pytest is implemented; anything else is rejected, never attempted.
`FakeAdapter` executes nothing and exists for deterministic unit tests.
"""
from __future__ import annotations
import re
import subprocess

from .models import ExecutionRejected

RESULT_RE = re.compile(r"^(\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")
SUMMARY_RE = re.compile(
    r"=+(\s*(\d+)\s+failed)?(,?\s*(\d+)\s+passed)?(,?\s*(\d+)\s+skipped)?"
    r"(,?\s*(\d+)\s+error)?[^=]*in\s+[\d.]+s\s*=+")
NO_TESTS_RE = re.compile(r"(no tests ran|no tests collected|collected 0 items)", re.I)
COLLECTED_RE = re.compile(r"collected (\d+) items?")


class TestRunnerAdapter:
    name = "base"

    def probe(self, python_exe: str) -> dict:
        raise NotImplementedError

    def build_argv(self, python_exe: str, selectors: list[str],
                   coverage_report: str = "") -> list[str]:
        raise NotImplementedError

    def parse_output(self, text: str, truncated: bool = False) -> dict:
        raise NotImplementedError


class PytestAdapter(TestRunnerAdapter):
    """Real pytest adapter. Argv is a fixed list; selectors are appended as
    separate arguments (never shell-joined). Parsing accepts `-v
    --tb=short` output only; anything else degrades to partial/failed parse
    status, never invented outcomes."""
    name = "pytest"

    def probe(self, python_exe: str) -> dict:
        try:
            r = subprocess.run([python_exe, "-m", "pytest", "--version"],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "version": "", "detail": f"probe failed: {e}"}
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"pytest\s+([\w.\-+]+)", out)
        if r.returncode == 0 and m:
            return {"ok": True, "version": m.group(1), "detail": out.strip()[:200]}
        return {"ok": False, "version": "", "detail": out.strip()[:200] or
                f"exit {r.returncode}"}

    def probe_cov(self, python_exe: str) -> dict:
        try:
            r = subprocess.run(
                [python_exe, "-c", "import pytest_cov, coverage; "
                 "print(pytest_cov.__version__ if hasattr(pytest_cov, '__version__') else 'ok')"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "detail": f"probe failed: {e}"}
        if r.returncode == 0:
            return {"ok": True, "detail": (r.stdout or '').strip()[:100]}
        return {"ok": False, "detail": (r.stderr or '').strip()[:200]}

    def build_argv(self, python_exe: str, selectors: list[str],
                   coverage_report: str = "") -> list[str]:
        if not selectors:
            raise ExecutionRejected("no tests selected")
        argv = [python_exe, "-m", "pytest", "-v", "--tb=short",
                "-p", "no:cacheprovider"]
        if coverage_report:
            argv += ["--cov=.", "--cov-report", f"json:{coverage_report}"]
        argv += list(selectors)
        return argv

    def parse_output(self, text: str, truncated: bool = False) -> dict:
        per_test: list[dict] = []
        seen: dict[str, str] = {}
        for line in (text or "").splitlines():
            m = RESULT_RE.match(line.strip())
            if not m:
                continue
            nodeid, outcome = m.group(1), m.group(2)
            if nodeid in seen:
                continue
            seen[nodeid] = outcome
            per_test.append({"node": nodeid, "outcome": outcome.lower()})
        summary = None
        for line in (text or "").splitlines():
            m = SUMMARY_RE.search(line)
            if m:
                summary = {"failed": int(m.group(2) or 0),
                           "passed": int(m.group(4) or 0),
                           "skipped": int(m.group(6) or 0),
                           "error": int(m.group(8) or 0)}
        collected = None
        for line in (text or "").splitlines():
            m = COLLECTED_RE.search(line)
            if m:
                collected = int(m.group(1))
        no_tests = bool(NO_TESTS_RE.search(text or ""))
        counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
        for p in per_test:
            o = p["outcome"]
            if o == "passed":
                counts["passed"] += 1
            elif o in ("failed", "error"):
                counts["failed" if o == "failed" else "error"] += 1
            elif o == "skipped":
                counts["skipped"] += 1
            # xfail/xpass: reported for information, not counted as pass/fail
        if summary is not None:
            parse_status = "ok"
        elif per_test and truncated:
            parse_status = "partial"
        elif per_test:
            # result lines but no summary line (e.g. killed mid-report)
            parse_status = "partial"
        else:
            parse_status = "failed"
        if collected is None:
            collected = len(per_test) if (summary is not None or per_test) else 0
        return {"per_test": per_test, "summary": summary, "collected": collected,
                "counts": counts, "no_tests": no_tests,
                "parse_status": parse_status, "truncated": truncated}


class FakeAdapter(TestRunnerAdapter):
    """Deterministic scripted adapter for unit tests. Runs no subprocess."""
    name = "fake"

    def __init__(self, script: dict | None = None):
        self.script = script or {}

    def probe(self, python_exe: str) -> dict:
        return {"ok": True, "version": "fake-1.0", "detail": "fake adapter"}

    def probe_cov(self, python_exe: str) -> dict:
        return {"ok": bool(self.script.get("cov_ok")), "detail": "fake"}

    def build_argv(self, python_exe: str, selectors: list[str],
                   coverage_report: str = "") -> list[str]:
        if not selectors:
            raise ExecutionRejected("no tests selected")
        return ["fake-pytest"] + list(selectors)

    def parse_output(self, text: str, truncated: bool = False) -> dict:
        if "output" in self.script:
            text = self.script["output"]
            truncated = bool(self.script.get("truncated"))
        return PytestAdapter().parse_output(text, truncated)


def get_adapter(name: str, fake_script: dict | None = None) -> TestRunnerAdapter:
    if name == "pytest":
        return PytestAdapter()
    if name == "fake":
        return FakeAdapter(fake_script)
    raise ExecutionRejected(
        f"unsupported runner: {name!r} (supported: 'pytest'; "
        "jest/vitest/mocha are not implemented in this phase)")
