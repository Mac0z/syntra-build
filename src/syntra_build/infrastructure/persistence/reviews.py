# ruff: noqa: E501
"""Durable, preservation-oriented Architect review evidence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from syntra_build.application.architect import ArchitectFailureKind
from syntra_build.domain.reviews import (
    ArchitectReview,
    ArchitectReviewRequest,
    ArchitectReviewVerdict,
)


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    id: str
    pull_request_id: str
    reviewed_sha: str
    verdict: ArchitectReviewVerdict
    finding_count: int
    superseded_at: datetime | None


class SQLiteArchitectReviewRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def begin(
        self,
        request_id: str,
        request: ArchitectReviewRequest,
        provider: str,
        model: str,
        reasoning_effort: str,
        now: datetime,
    ) -> None:
        self.connection.execute(
            """INSERT INTO architect_requests
            (id,project_id,milestone_id,request_type,provider,model,reasoning_level,
             request_schema_version,request_payload_json,correlation_id,started_at,status)
            VALUES (?,?,?,'REVIEW',?,?,?,?,?,?,?,'STARTED')""",
            (
                request_id,
                str(request.project_id),
                str(request.milestone_id),
                provider,
                model,
                reasoning_effort,
                request.interface_version,
                json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":")),
                request.correlation_id,
                now.isoformat(),
            ),
        )

    def fail(self, request_id: str, kind: ArchitectFailureKind, now: datetime) -> None:
        changed = self.connection.execute(
            "UPDATE architect_requests SET status='FAILED',completed_at=?,failure_classification=? WHERE id=? AND status='STARTED'",
            (now.isoformat(), kind.value, request_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Architect review request is unavailable or completed")

    def complete(
        self,
        request_id: str,
        pull_request_id: str,
        review: ArchitectReview,
        provider_response_id: str | None,
        usage: dict[str, int | str | None],
        now: datetime,
        *,
        superseded: bool = False,
    ) -> ReviewRecord:
        row = self.connection.execute(
            "SELECT provider,model,status FROM architect_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        if row is None or row["status"] != "STARTED":
            existing = self.connection.execute(
                "SELECT r.*,(SELECT count(*) FROM architect_review_findings f WHERE f.review_id=r.id) AS finding_count FROM architect_reviews r WHERE r.architect_request_id=?",
                (request_id,),
            ).fetchone()
            if existing is None:
                raise RuntimeError(
                    "Architect review request is unavailable or completed"
                )
            return self._record(existing)
        review_id = str(uuid4())
        self.connection.execute(
            """INSERT INTO architect_responses
            (id,architect_request_id,response_type,response_schema_version,normalised_payload_json,status,created_at,validation_status,provider,model,input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,total_tokens)
            VALUES (?,?,'REVIEW',?,?,'ACCEPTED',?,'VALID',?,?,?,?,?,?,?)""",
            (
                str(uuid4()),
                request_id,
                review.interface_version,
                json.dumps(review.to_dict(), sort_keys=True, separators=(",", ":")),
                now.isoformat(),
                row["provider"],
                row["model"],
                usage.get("input_tokens"),
                usage.get("cached_input_tokens"),
                usage.get("output_tokens"),
                usage.get("reasoning_tokens"),
                usage.get("total_tokens"),
            ),
        )
        self.connection.execute(
            """INSERT INTO architect_reviews
            (id,project_id,milestone_id,architect_request_id,pull_request_id,reviewed_sha,verdict,summary,created_at,superseded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                review_id,
                str(review.project_id),
                str(review.milestone_id),
                request_id,
                pull_request_id,
                review.reviewed_sha,
                review.verdict.value,
                review.summary,
                now.isoformat(),
                now.isoformat() if superseded else None,
            ),
        )
        for finding in review.findings:
            self.connection.execute(
                """INSERT INTO architect_review_findings
                (id,review_id,finding_code,severity,requirement_ref,description,recommended_action,status,resolved_by_review_id,created_at)
                VALUES (?,?,?,?,?,?,?,'OPEN',NULL,?)""",
                (
                    str(uuid4()),
                    review_id,
                    finding.finding_id,
                    finding.severity.value,
                    finding.requirement_ref,
                    finding.description,
                    finding.recommended_action,
                    now.isoformat(),
                ),
            )
        self.connection.execute(
            "UPDATE architect_requests SET status='SUCCEEDED',completed_at=?,external_request_id=? WHERE id=?",
            (now.isoformat(), provider_response_id, request_id),
        )
        return ReviewRecord(
            review_id,
            pull_request_id,
            review.reviewed_sha,
            review.verdict,
            len(review.findings),
            now if superseded else None,
        )

    def latest(
        self, project_id: str, milestone_id: str, pull_request_id: str
    ) -> ReviewRecord | None:
        row = self.connection.execute(
            """SELECT r.*,(SELECT count(*) FROM architect_review_findings f WHERE f.review_id=r.id) AS finding_count FROM architect_reviews r WHERE r.project_id=? AND r.milestone_id=? AND r.pull_request_id=? ORDER BY r.created_at DESC,r.rowid DESC LIMIT 1""",
            (project_id, milestone_id, pull_request_id),
        ).fetchone()
        return None if row is None else self._record(row)

    def completed_for_correlation(
        self, project_id: str, milestone_id: str, correlation_id: str
    ) -> ReviewRecord | None:
        row = self.connection.execute(
            """SELECT r.*,(SELECT count(*) FROM architect_review_findings f
            WHERE f.review_id=r.id) AS finding_count
            FROM architect_reviews r JOIN architect_requests q
              ON q.id=r.architect_request_id
            WHERE r.project_id=? AND r.milestone_id=? AND q.correlation_id=?
            ORDER BY r.created_at DESC,r.rowid DESC LIMIT 1""",
            (project_id, milestone_id, correlation_id),
        ).fetchone()
        return None if row is None else self._record(row)

    def previous_open_findings(
        self, project_id: str, milestone_id: str, pull_request_id: str
    ) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.connection.execute(
                """SELECT f.* FROM architect_review_findings f JOIN architect_reviews r ON r.id=f.review_id WHERE r.project_id=? AND r.milestone_id=? AND r.pull_request_id=? AND f.status IN ('OPEN','ACCEPTED') ORDER BY f.created_at,f.id""",
                (project_id, milestone_id, pull_request_id),
            ).fetchall()
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> ReviewRecord:
        return ReviewRecord(
            row["id"],
            row["pull_request_id"],
            row["reviewed_sha"],
            ArchitectReviewVerdict(row["verdict"]),
            int(row["finding_count"]),
            datetime.fromisoformat(row["superseded_at"]).astimezone(UTC)
            if row["superseded_at"]
            else None,
        )
