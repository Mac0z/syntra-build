"""Durable M20 coding-worker invocation evidence."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from uuid import uuid4

from syntra_build.domain.codex import CodexRunRequest, CodexRunResult, ReportedTest


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
