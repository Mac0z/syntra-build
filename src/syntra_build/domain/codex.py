"""Provider-neutral contracts for one untrusted coding-worker invocation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId


class CodexProcessStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"


@dataclass(frozen=True, slots=True)
class ReportedTest:
    command: str
    status: str
    summary: str | None = None
    duration_ms: int | None = None
    output_reference: str | None = None


@dataclass(frozen=True, slots=True)
class CodexRunRequest:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    job_id: JobId
    attempt_number: int
    worktree_path: Path
    task: Mapping[str, object]
    agents_markdown: str
    timeout_seconds: float
    previous_run_summary: str | None = None

    def __post_init__(self) -> None:
        if self.interface_version != "1.0":
            raise ValueError("unsupported Codex run interface version")
        if not self.correlation_id.strip() or self.attempt_number < 1:
            raise ValueError("Codex run identity is invalid")
        if not self.task or not self.agents_markdown.strip():
            raise ValueError("Codex task and engineering instructions are required")
        if self.timeout_seconds <= 0:
            raise ValueError("Codex timeout must be positive")


@dataclass(frozen=True, slots=True)
class CodexRunResult:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    job_id: JobId
    attempt_number: int
    process_status: CodexProcessStatus
    started_at: datetime
    completed_at: datetime
    worker_identity: str
    timeout_seconds: float
    process_id: int | None = None
    exit_code: int | None = None
    stdout_reference: str | None = None
    stderr_reference: str | None = None
    summary: str = ""
    tests_run: tuple[ReportedTest, ...] = field(default_factory=tuple)
    known_issues: tuple[str, ...] = field(default_factory=tuple)
