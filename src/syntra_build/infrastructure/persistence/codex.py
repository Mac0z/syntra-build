"""Durable M20 coding-worker invocation evidence."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.domain.codex import (
    CodexProcessStatus,
    CodexRunRequest,
    CodexRunResult,
    ReportedTest,
)
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId


class SQLiteCodexRunRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def start(
        self,
        run_id: str,
        request: CodexRunRequest,
        worktree_id: str,
        task_hash: str,
        started_at: datetime,
        stdout_reference: str,
        stderr_reference: str,
    ) -> None:
        self.connection.execute(
            """INSERT INTO codex_runs
            (id,project_id,milestone_id,job_id,worktree_id,correlation_id,
             interface_version,attempt_number,task_payload_json,task_content_hash,
             process_status,worker_identity,started_at,timeout_seconds,
             stdout_reference,stderr_reference,result_metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,'RUNNING','pending',?,?,?,?, '{}')""",
            (
                run_id,
                str(request.project_id),
                str(request.milestone_id),
                str(request.job_id),
                worktree_id,
                request.correlation_id,
                request.interface_version,
                request.attempt_number,
                json.dumps(request.task, sort_keys=True, separators=(",", ":")),
                task_hash,
                started_at.isoformat(),
                request.timeout_seconds,
                stdout_reference,
                stderr_reference,
            ),
        )
        self.connection.commit()

    def record_process(self, run_id: str, process_id: int, worker: str) -> None:
        self.connection.execute(
            """UPDATE codex_runs SET process_id=?,worker_identity=?
            WHERE id=? AND process_status='RUNNING'""",
            (process_id, worker, run_id),
        )
        self.connection.commit()

    def complete(self, run_id: str, result: CodexRunResult) -> None:
        metadata = json.dumps(
            {
                "tests_reported": len(result.tests_run),
                "known_issue_count": len(result.known_issues),
            },
            sort_keys=True,
        )
        self.connection.execute(
            """UPDATE codex_runs SET process_status=?,completed_at=?,exit_code=?,
            summary=?,known_issues_json=?,result_metadata_json=?
            WHERE id=? AND process_status='RUNNING'""",
            (
                result.process_status.value,
                result.completed_at.isoformat(),
                result.exit_code,
                result.summary,
                json.dumps(result.known_issues),
                metadata,
                run_id,
            ),
        )
        for report in result.tests_run:
            self._test(run_id, report)
        self.connection.commit()

    def completed_for_request(self, request: CodexRunRequest) -> CodexRunResult | None:
        """Return exact durable terminal evidence, rejecting mismatched identity."""
        row = self.connection.execute(
            """SELECT r.*,w.worktree_path FROM codex_runs r
               JOIN git_workspaces w ON w.id=r.worktree_id
               WHERE r.job_id=? AND r.attempt_number=?""",
            (str(request.job_id), request.attempt_number),
        ).fetchone()
        if row is None:
            return None
        expected_task = json.dumps(request.task, sort_keys=True, separators=(",", ":"))
        if (
            row["project_id"] != str(request.project_id)
            or row["milestone_id"] != str(request.milestone_id)
            or row["correlation_id"] != request.correlation_id
            or row["interface_version"] != request.interface_version
            or row["task_payload_json"] != expected_task
            or Path(row["worktree_path"]).resolve(strict=False)
            != request.worktree_path.resolve(strict=False)
            or row["timeout_seconds"] != request.timeout_seconds
        ):
            raise ValueError("durable Codex run identity does not match request")
        if row["completed_at"] is None:
            raise ValueError("existing Codex run attempt is not terminal")
        return CodexRunResult(
            row["interface_version"],
            row["correlation_id"],
            ProjectId.from_string(row["project_id"]),
            MilestoneId.from_string(row["milestone_id"]),
            JobId.from_string(row["job_id"]),
            row["attempt_number"],
            CodexProcessStatus(row["process_status"]),
            datetime.fromisoformat(row["started_at"]).astimezone(UTC),
            datetime.fromisoformat(row["completed_at"]).astimezone(UTC),
            row["worker_identity"],
            row["timeout_seconds"],
            row["process_id"],
            row["exit_code"],
            row["stdout_reference"],
            row["stderr_reference"],
            row["summary"] or "",
            known_issues=tuple(json.loads(row["known_issues_json"])),
        )

    def _test(self, run_id: str, report: ReportedTest) -> None:
        self.connection.execute(
            """INSERT INTO codex_run_tests
            (id,codex_run_id,command,status,summary,duration_ms,output_reference)
            VALUES (?,?,?,?,?,?,?)""",
            (
                str(uuid4()),
                run_id,
                report.command,
                report.status,
                report.summary,
                report.duration_ms,
                report.output_reference,
            ),
        )
