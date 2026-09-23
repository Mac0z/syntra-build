# ruff: noqa: E501
"""Durable, exact-revision CI evidence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIFailureClassification,
    CIOverallStatus,
)


@dataclass(frozen=True, slots=True)
class CIRunRecord:
    id: str
    project_id: str
    milestone_id: str
    pull_request_id: str
    head_sha: str
    overall_status: CIOverallStatus
    failure_classification: CIFailureClassification | None
    started_at: datetime
    last_checked_at: datetime
    retry_count: int
    next_check_at: datetime | None
    checks: tuple[CICheck, ...]


class SQLiteCIRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def reconcile(
        self,
        project_id: str,
        milestone_id: str,
        pull_request_id: str,
        head_sha: str,
        status: CIOverallStatus,
        checks: tuple[CICheck, ...],
        workflow_ids: tuple[str, ...],
        classification: CIFailureClassification | None,
        now: datetime,
        *,
        retry_count: int = 0,
        next_check_at: datetime | None = None,
        summary: dict[str, object] | None = None,
    ) -> CIRunRecord:
        row = self.connection.execute(
            "SELECT * FROM ci_runs WHERE pull_request_id=? AND head_sha=?",
            (pull_request_id, head_sha),
        ).fetchone()
        completed = (
            now.isoformat()
            if status
            in {
                CIOverallStatus.PASSED,
                CIOverallStatus.FAILED,
                CIOverallStatus.CANCELLED,
            }
            else None
        )
        payload = json.dumps(summary or {}, sort_keys=True, separators=(",", ":"))
        if row is None:
            run_id = str(uuid4())
            self.connection.execute(
                """INSERT INTO ci_runs
                (id,project_id,milestone_id,pull_request_id,head_sha,overall_status,
                 failure_classification,external_workflow_run_id,started_at,completed_at,
                 last_checked_at,summary_json,retry_count,next_check_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    project_id,
                    milestone_id,
                    pull_request_id,
                    head_sha,
                    status,
                    classification,
                    ",".join(workflow_ids) or None,
                    now.isoformat(),
                    completed,
                    now.isoformat(),
                    payload,
                    retry_count,
                    next_check_at.isoformat() if next_check_at else None,
                ),
            )
        else:
            if (row["project_id"], row["milestone_id"]) != (project_id, milestone_id):
                raise sqlite3.IntegrityError(
                    "CI identity conflicts with pull request ownership"
                )
            run_id = row["id"]
            # A terminal run may be observed again but never regressed.
            previous = CIOverallStatus(row["overall_status"])
            if (
                previous
                in {
                    CIOverallStatus.PASSED,
                    CIOverallStatus.FAILED,
                    CIOverallStatus.CANCELLED,
                }
                and status != previous
            ):
                raise sqlite3.IntegrityError("terminal CI evidence cannot regress")
            self.connection.execute(
                """UPDATE ci_runs SET overall_status=?,failure_classification=?,
                external_workflow_run_id=?,completed_at=COALESCE(completed_at,?),last_checked_at=?,
                summary_json=?,retry_count=?,next_check_at=? WHERE id=?""",
                (
                    status,
                    classification,
                    ",".join(workflow_ids) or None,
                    completed,
                    now.isoformat(),
                    payload,
                    retry_count,
                    next_check_at.isoformat() if next_check_at else None,
                    run_id,
                ),
            )
        for check in checks:
            check_row = self.connection.execute(
                "SELECT * FROM ci_checks WHERE ci_run_id=? AND external_check_id=?",
                (run_id, check.external_check_id),
            ).fetchone()
            values = (
                check.name,
                check.status,
                check.conclusion,
                check.started_at,
                check.completed_at,
                check.details_url,
                check.failure_summary,
            )
            if check_row is None:
                self.connection.execute(
                    """INSERT INTO ci_checks
                    (id,ci_run_id,name,external_check_id,status,conclusion,started_at,
                     completed_at,details_url,failure_summary) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid4()),
                        run_id,
                        check.name,
                        check.external_check_id,
                        *values[1:],
                    ),
                )
            else:
                old_status = CICheckStatus(check_row["status"])
                if (
                    old_status is CICheckStatus.COMPLETED
                    and check.status is not CICheckStatus.COMPLETED
                ):
                    raise sqlite3.IntegrityError("completed CI check cannot regress")
                self.connection.execute(
                    """UPDATE ci_checks SET name=?,status=?,conclusion=?,
                    started_at=COALESCE(started_at,?),completed_at=COALESCE(completed_at,?),
                    details_url=?,failure_summary=? WHERE ci_run_id=? AND external_check_id=?""",
                    (*values, run_id, check.external_check_id),
                )
        return self.get(run_id)

    def get(self, run_id: str) -> CIRunRecord:
        row = self.connection.execute(
            "SELECT * FROM ci_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise LookupError("CI run does not exist")
        checks = tuple(
            CICheck(
                item["name"],
                item["external_check_id"],
                CICheckStatus(item["status"]),
                CICheckConclusion(item["conclusion"]) if item["conclusion"] else None,
                item["started_at"],
                item["completed_at"],
                item["details_url"],
                item["failure_summary"],
            )
            for item in self.connection.execute(
                "SELECT * FROM ci_checks WHERE ci_run_id=? ORDER BY name,external_check_id",
                (run_id,),
            )
        )

        def parse(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value).astimezone(UTC) if value else None

        started_at = parse(row["started_at"])
        last_checked_at = parse(row["last_checked_at"])
        assert started_at is not None and last_checked_at is not None
        return CIRunRecord(
            row["id"],
            row["project_id"],
            row["milestone_id"],
            row["pull_request_id"],
            row["head_sha"],
            CIOverallStatus(row["overall_status"]),
            CIFailureClassification(row["failure_classification"])
            if row["failure_classification"]
            else None,
            started_at,
            last_checked_at,
            row["retry_count"],
            parse(row["next_check_at"]),
            checks,
        )

    def latest(self, milestone_id: str) -> CIRunRecord | None:
        row = self.connection.execute(
            "SELECT id FROM ci_runs WHERE milestone_id=? ORDER BY started_at DESC,id DESC LIMIT 1",
            (milestone_id,),
        ).fetchone()
        return self.get(row["id"]) if row else None
