"""Discovery and append-only persistence for startup recovery."""

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime

from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.recovery import RecoveryDecision, RecoverySubject


class SQLiteRecoveryRepository:
    def __init__(self, connection: sqlite3.Connection, id_factory: Callable[[], str]):
        self.connection, self.id_factory = connection, id_factory

    def begin(self, correlation_id: str, now: datetime) -> str:
        run_id = self.id_factory()
        self.connection.execute(
            "INSERT INTO recovery_runs VALUES (?,?,'RECOVERING',?,NULL,NULL)",
            (run_id, correlation_id, now.isoformat()),
        )
        return run_id

    def finish(self, run_id: str, now: datetime, error: str | None = None) -> None:
        self.connection.execute(
            """UPDATE recovery_runs SET status=?,completed_at=?,error_detail=?
               WHERE id=? AND status='RECOVERING'""",
            ("FAILED" if error else "READY", now.isoformat(), error, run_id),
        )

    def discover(self) -> tuple[RecoverySubject, ...]:
        milestones = self.connection.execute(
            """SELECT p.id project_id,p.state project_state,
               m.id milestone_id,m.state milestone_state
               FROM projects p JOIN milestones m ON m.project_id=p.id
               WHERE p.state NOT IN ('COMPLETE','FAILED','CANCELLED')
                 AND m.state NOT IN ('PENDING','COMPLETE','FAILED','CANCELLED')
               ORDER BY p.created_at,p.id,m.sequence_number,m.id"""
        ).fetchall()
        jobs = self.connection.execute(
            """SELECT p.id project_id,p.state project_state,
               m.id milestone_id,m.state milestone_state,j.id job_id,j.state job_state
               FROM jobs j JOIN projects p ON p.id=j.project_id
               LEFT JOIN milestones m ON m.id=j.milestone_id
               WHERE p.state NOT IN ('COMPLETE','FAILED','CANCELLED')
                 AND j.state IN ('DISPATCHED','RUNNING','WAITING_EXTERNAL','RETRY_WAIT')
               ORDER BY p.created_at,p.id,j.created_at,j.id"""
        ).fetchall()
        subjects = [
            RecoverySubject(
                ProjectId.from_string(row["project_id"]),
                row["project_state"],
                MilestoneId.from_string(row["milestone_id"]),
                row["milestone_state"],
            )
            for row in milestones
        ]
        subjects.extend(
            RecoverySubject(
                ProjectId.from_string(row["project_id"]),
                row["project_state"],
                MilestoneId.from_string(row["milestone_id"])
                if row["milestone_id"]
                else None,
                row["milestone_state"],
                JobId.from_string(row["job_id"]),
                row["job_state"],
            )
            for row in jobs
        )
        return tuple(subjects)

    def observe(
        self,
        run_id: str,
        subject: RecoverySubject,
        decision: RecoveryDecision,
        correlation_id: str,
        now: datetime,
    ) -> None:
        self.connection.execute(
            """INSERT INTO recovery_observations
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.id_factory(),
                run_id,
                str(subject.project_id),
                str(subject.milestone_id) if subject.milestone_id else None,
                str(subject.job_id) if subject.job_id else None,
                decision.category,
                subject.persisted_state,
                json.dumps(
                    dict(decision.observed), sort_keys=True, separators=(",", ":")
                ),
                decision.disposition.value,
                decision.action,
                correlation_id,
                decision.reason,
                now.isoformat(),
            ),
        )

    def observations(self, run_id: str) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.connection.execute(
                """SELECT * FROM recovery_observations WHERE recovery_run_id=?
                   ORDER BY observed_at,id""",
                (run_id,),
            ).fetchall()
        )
