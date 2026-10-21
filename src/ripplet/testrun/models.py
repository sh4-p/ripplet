"""Test execution request + run record models (Phase 10 Stages 2/6).

A request is explicit authorization paperwork: repository, plan, exact tests,
runner, bounds, and an authorization record. Nothing here starts a process.
A run record is the durable outcome; per-test outcomes come only from parsed
runner output, never from exit codes or string absence.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field, asdict
from typing import Any

SUPPORTED_RUNNERS = ("pytest",)
DEFAULT_RUNNER = "pytest"

OUTCOMES = ("passed", "failed", "partial", "timed_out", "cancelled",
            "runner_error", "result_parse_error")
PARSE_STATUSES = ("ok", "partial", "failed")

AUTH_METHODS = ("cli-confirm", "cli-yes-flag")

DEFAULTS = {
    "timeout_s": 300.0,
    "max_output_bytes": 1_000_000,
    "max_tests": 100,
    "max_concurrent_runs": 1,
    "retention_runs": 100,
    "retention_plans": 200,
}


class ExecutionRejected(ValueError):
    """Safe, user-facing refusal to build/authorize/execute a request."""


@dataclass
class SelectedTest:
    test_id: str
    selector: str
    file: str
    display: str = ""

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SelectedTest":
        return SelectedTest(test_id=d.get("test_id", ""), selector=d.get("selector", ""),
                            file=d.get("file", ""), display=d.get("display", ""))


@dataclass
class ExecutionRequest:
    repo_id: str
    repo_root: str
    plan_id: str
    selection: list[SelectedTest] = field(default_factory=list)
    runner: str = DEFAULT_RUNNER
    python_exe: str = ""
    runner_version: str = ""
    timeout_s: float = DEFAULTS["timeout_s"]
    max_output_bytes: int = DEFAULTS["max_output_bytes"]
    coverage: bool = False
    coverage_tool: str = ""
    source_revision: str = ""
    source_fingerprint: dict[str, str] = field(default_factory=dict)
    authorization: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def authorized(self) -> bool:
        return bool(self.authorization) and \
            self.authorization.get("method") in AUTH_METHODS

    def to_dict(self):
        d = asdict(self)
        d["selection"] = [s.to_dict() if isinstance(s, SelectedTest) else s
                          for s in self.selection]
        return d


@dataclass
class TestRunRecord:
    id: str
    plan_id: str
    repo_id: str
    project_id: str = "default"
    source_revision: str = ""
    source_fingerprint: dict[str, str] = field(default_factory=dict)
    runner: str = DEFAULT_RUNNER
    runner_version: str = ""
    selection: list[dict] = field(default_factory=list)
    timeout_s: float = DEFAULTS["timeout_s"]
    max_output_bytes: int = DEFAULTS["max_output_bytes"]
    coverage_enabled: bool = False
    coverage_run_id: str = ""
    coverage_attribution: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_ms: float = 0.0
    exit_code: Any = None
    outcome: str = "runner_error"
    parse_status: str = "failed"
    collected: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    unreported: int = 0
    per_test: list[dict] = field(default_factory=list)
    output_bytes: int = 0
    output_truncated: bool = False
    log_path: str = ""
    authorization: dict[str, Any] = field(default_factory=dict)
    config_fps: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return asdict(self)

    def summary(self) -> dict:
        return {"id": self.id, "plan_id": self.plan_id, "repo_id": self.repo_id,
                "runner": self.runner, "outcome": self.outcome,
                "exit_code": self.exit_code, "collected": self.collected,
                "passed": self.passed, "failed": self.failed,
                "skipped": self.skipped, "unreported": self.unreported,
                "parse_status": self.parse_status,
                "duration_ms": self.duration_ms,
                "coverage_run_id": self.coverage_run_id,
                "output_bytes": self.output_bytes,
                "output_truncated": self.output_truncated}
