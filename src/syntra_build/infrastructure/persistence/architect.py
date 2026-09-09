# ruff: noqa: E501
"""Durable audit records for Architect calls and outcomes."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from syntra_build.domain import ArchitectDesignRequest, ArchitectDesignResponse
from syntra_build.infrastructure.persistence.errors import PersistenceError

if TYPE_CHECKING:
    from syntra_build.application.architect import ArchitectFailureKind


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.isoformat(timespec="microseconds")


class SQLiteArchitectInteractionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def begin(
        self,
        *,
        request_id: str,
        request: ArchitectDesignRequest,
        provider: str,
        model: str,
        reasoning_effort: str,
        created_at: datetime,
    ) -> None:
        try:
            self._connection.execute(
                """INSERT INTO architect_requests (id,project_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,status) VALUES (?,?, 'DESIGN',?,?,?,?,?,?,?,'STARTED')""",
                (
                    request_id,
                    str(request.project_id),
                    provider,
                    model,
                    reasoning_effort,
                    request.interface_version,
                    json.dumps(
                        request.to_dict(), sort_keys=True, separators=(",", ":")
                    ),
                    request.correlation_id,
                    _time(created_at),
                ),
            )
        except sqlite3.Error as error:
            raise PersistenceError(
                "Architect request could not be persisted"
            ) from error

    def succeed(
        self,
        *,
        request_id: str,
        response: ArchitectDesignResponse,
        provider_response_id: str | None,
        usage: dict[str, int | str | None],
        completed_at: datetime,
    ) -> None:
        try:
            row = self._connection.execute(
                "SELECT provider,model,status FROM architect_requests WHERE id=?",
                (request_id,),
            ).fetchone()
            if row is None or row["status"] != "STARTED":
                raise PersistenceError("Architect request is unavailable or completed")
            self._connection.execute("BEGIN")
            self._connection.execute(
                """INSERT INTO architect_responses (id,architect_request_id,response_type,response_schema_version,normalised_payload_json,status,created_at,validation_status,provider,model,input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,total_tokens) VALUES (?,?,'DESIGN',?,?,'ACCEPTED',?,'VALID',?,?,?,?,?,?,?)""",
                (
                    str(uuid4()),
                    request_id,
                    response.interface_version,
                    json.dumps(
                        response.to_dict(), sort_keys=True, separators=(",", ":")
                    ),
                    _time(completed_at),
                    row["provider"],
                    row["model"],
                    usage.get("input_tokens"),
                    usage.get("cached_input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("reasoning_tokens"),
                    usage.get("total_tokens"),
                ),
            )
            self._connection.execute(
                "UPDATE architect_requests SET status='SUCCEEDED',completed_at=?,external_request_id=? WHERE id=? AND status='STARTED'",
                (_time(completed_at), provider_response_id, request_id),
            )
            self._connection.commit()
        except sqlite3.Error, PersistenceError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def fail(
        self, *, request_id: str, kind: ArchitectFailureKind, completed_at: datetime
    ) -> None:
        changed = self._connection.execute(
            "UPDATE architect_requests SET status='FAILED',completed_at=?,failure_classification=? WHERE id=? AND status='STARTED'",
            (_time(completed_at), kind.value, request_id),
        ).rowcount
        if changed != 1:
            raise PersistenceError("Architect failure could not be persisted")

    def history(self, project_id: str) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._connection.execute(
                "SELECT * FROM architect_requests WHERE project_id=? ORDER BY started_at,id",
                (project_id,),
            ).fetchall()
        )
