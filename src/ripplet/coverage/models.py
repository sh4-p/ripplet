from __future__ import annotations
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

REVISION_STATUSES = ("verified", "unverified", "override", "historical", "stale")
RUN_STATUSES = ("complete", "partial", "failed")
CONFIDENCES = ("exact", "narrowest", "ambiguous", "file_only")


@dataclass
class CoverageRun:
    id: str
    repo_id: str = ""
    project_id: str = "default"
    tool: str = ""
    tool_version: str = ""
    format: str = ""
    format_version: str = ""
    report_hash: str = ""
    generated_at: Optional[float] = None
    imported_at: float = field(default_factory=time.time)
    git_rev: str = ""
    git_branch: str = ""
    revision_status: str = "unverified"
    source_fps: dict[str, str] = field(default_factory=dict)
    has_line: bool = False
    has_func: bool = False
    has_branch: bool = False
    has_contexts: bool = False
    label: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    status: str = "complete"
    diagnostics: list[str] = field(default_factory=list)
    def to_dict(self): return asdict(self)


@dataclass
class FileObs:
    file: str
    statements_found: Optional[int] = None
    statements_covered: Optional[int] = None
    lines_hit: int = 0
    lines_missed: int = 0
    funcs_found: Optional[int] = None
    funcs_covered: Optional[int] = None
    branches_found: Optional[int] = None
    branches_taken: Optional[int] = None
    match_status: str = "mapped"


@dataclass
class LineObs:
    file: str
    line: int
    hits: int


@dataclass
class BranchObs:
    file: str
    line: int
    block: Optional[int] = None
    branch: Optional[int] = None
    taken: int = 0


@dataclass
class FuncObs:
    file: str
    name: str
    line: Optional[int] = None
    hits: int = 0


@dataclass
class CtxObs:
    context: str
    file: str
    line: int
    hits: int


@dataclass
class ParsedReport:
    run: CoverageRun
    files: list[FileObs] = field(default_factory=list)
    lines: list[LineObs] = field(default_factory=list)
    branches: list[BranchObs] = field(default_factory=list)
    funcs: list[FuncObs] = field(default_factory=list)
    contexts: list[CtxObs] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    partial: bool = False
    adapter: str = ""
    adapter_version: str = "1"
