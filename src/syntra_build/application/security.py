"""Durable security event recording and active-condition policy."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.security import (
    SecurityActorType,
    SecurityEvent,
    SecurityEventResolution,
    SecurityEventType,
    SecurityResolutionCode,
    SecuritySeverity,
)
from syntra_build.infrastructure.persistence.connection import transaction_scope

_UNSAFE_KEY = re.compile(r"(?:secret|token|password|authorization|private.?key)", re.I)
_UNSAFE_VALUE = re.compile(
    r"(?:gh[pousr]_|github_pat_|-----BEGIN .*PRIVATE KEY-----|https?://[^\s/:]+:[^\s/@]+@)",
    re.I,
)


class UnsafeSecurityDetails(ValueError):
    """Security audit details could contain credential material."""


class SecurityPolicy:
    """SQLite is the sole authority for active security conditions."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.connection = connection
        self.id_factory = id_factory or (lambda: str(uuid4()))

    @staticmethod
    def _safe(details: Mapping[str, str | int | bool | None]) -> str:
        for key, value in details.items():
            if _UNSAFE_KEY.search(key) or (
                isinstance(value, str) and _UNSAFE_VALUE.search(value)
            ):
                raise UnsafeSecurityDetails("security details must be non-sensitive")
        return json.dumps(dict(details), sort_keys=True, separators=(",", ":"))

    def record(
        self,
        event_type: SecurityEventType,
        severity: SecuritySeverity,
        *,
        source_component: str,
        correlation_id: str,
        project_id: ProjectId | None = None,
        milestone_id: MilestoneId | None = None,
        source_reference: str | None = None,
        safe_details: Mapping[str, str | int | bool | None] = {},
        blocking: bool = False,
        created_at: datetime | None = None,
    ) -> SecurityEvent:
        if blocking and severity not in {
            SecuritySeverity.HIGH,
            SecuritySeverity.CRITICAL,
        }:
            raise ValueError("only HIGH or CRITICAL events may be blocking")
        created_at = created_at or datetime.now(UTC)
        event = SecurityEvent(
            self.id_factory(),
            event_type,
            severity,
            project_id,
            milestone_id,
            source_component,
            source_reference,
            correlation_id,
            dict(safe_details),
            blocking,
            created_at,
        )
        encoded = self._safe(event.safe_details)
        with transaction_scope(self.connection):
            self.connection.execute(
                """INSERT INTO security_events
                (id,event_type,severity,project_id,milestone_id,source_component,
                 source_reference,correlation_id,safe_details_json,blocking,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.id,
                    event.event_type.value,
                    event.severity.value,
                    str(project_id) if project_id else None,
                    str(milestone_id) if milestone_id else None,
                    source_component,
                    source_reference,
                    correlation_id,
                    encoded,
                    int(blocking),
                    created_at.isoformat(),
                ),
            )
        return event

    def resolve(
        self,
        event_id: str,
        resolution_code: SecurityResolutionCode,
        correlation_id: str,
        actor_type: SecurityActorType,
        actor_id: str | None = None,
        resolved_at: datetime | None = None,
    ) -> SecurityEventResolution:
        if not isinstance(resolution_code, SecurityResolutionCode):
            raise ValueError("resolution code is not permitted")
        if not isinstance(actor_type, SecurityActorType):
            raise ValueError("security actor type is not permitted")
        if actor_id is not None:
            self._safe({"actor_id": actor_id})
        resolved_at = resolved_at or datetime.now(UTC)
        if (
            self.connection.execute(
                "SELECT 1 FROM security_events WHERE id=?", (event_id,)
            ).fetchone()
            is None
        ):
            raise ValueError("security event does not exist")
        resolution = SecurityEventResolution(
            self.id_factory(),
            event_id,
            resolution_code,
            correlation_id,
            actor_type,
            actor_id,
            resolved_at,
        )
        with transaction_scope(self.connection):
            self.connection.execute(
                """INSERT INTO security_event_resolutions
                (id,security_event_id,resolution_code,correlation_id,actor_type,
                 actor_id,resolved_at) VALUES(?,?,?,?,?,?,?)""",
                (
                    resolution.id,
                    event_id,
                    resolution_code.value,
                    correlation_id,
                    actor_type.value,
                    actor_id,
                    resolved_at.isoformat(),
                ),
            )
        return resolution

    def resolve_clean_revalidation(
        self,
        *,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        source_reference: str,
        active_types: set[SecurityEventType],
        correlation_id: str,
        resolved_at: datetime,
    ) -> None:
        """Resolve only vanished findings from the same trusted workspace scope."""
        rows = self.connection.execute(
            """SELECT e.id,e.event_type FROM security_events e
            LEFT JOIN security_event_resolutions r ON r.security_event_id=e.id
            WHERE e.project_id=? AND e.milestone_id=?
              AND e.source_component='change_validation'
              AND e.source_reference=? AND e.blocking=1 AND r.id IS NULL""",
            (str(project_id), str(milestone_id), source_reference),
        ).fetchall()
        for row in rows:
            if SecurityEventType(row["event_type"]) not in active_types:
                self.resolve(
                    row["id"],
                    SecurityResolutionCode.CLEAN_REVALIDATION,
                    correlation_id,
                    SecurityActorType.SYSTEM,
                    resolved_at=resolved_at,
                )

    def resolve_source_conditions(
        self,
        *,
        project_id: ProjectId,
        milestone_id: MilestoneId | None,
        source_component: str,
        source_reference: str,
        event_type: SecurityEventType,
        correlation_id: str,
        resolved_at: datetime,
    ) -> None:
        rows = self.connection.execute(
            """SELECT e.id FROM security_events e
            LEFT JOIN security_event_resolutions r ON r.security_event_id=e.id
            WHERE e.project_id=? AND e.milestone_id IS ? AND e.source_component=?
              AND e.source_reference=? AND e.event_type=? AND e.blocking=1
              AND r.id IS NULL""",
            (
                str(project_id),
                str(milestone_id) if milestone_id else None,
                source_component,
                source_reference,
                event_type.value,
            ),
        ).fetchall()
        for row in rows:
            self.resolve(
                row["id"],
                SecurityResolutionCode.CLEAN_REVALIDATION,
                correlation_id,
                SecurityActorType.SYSTEM,
                resolved_at=resolved_at,
            )

    def blocked(
        self, project_id: ProjectId, milestone_id: MilestoneId | None = None
    ) -> bool:
        # Migration/upgrade inspection can intentionally operate on a pre-M31
        # schema. Absence of the table is not an active event; production
        # bootstrap applies all migrations before privileged dispatch.
        exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='security_events'"
        ).fetchone()
        if exists is None:
            return False
        row = self.connection.execute(
            """SELECT 1 FROM security_events e
            LEFT JOIN security_event_resolutions r ON r.security_event_id=e.id
            WHERE e.blocking=1 AND e.severity IN ('HIGH','CRITICAL')
              AND r.id IS NULL AND (e.project_id IS NULL OR e.project_id=?)
              AND (e.milestone_id IS NULL OR e.milestone_id=?) LIMIT 1""",
            (str(project_id), str(milestone_id) if milestone_id else None),
        ).fetchone()
        return row is not None

    (SecurityResolutionCode,)
