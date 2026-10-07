"""Notification-only M32 handoff for already committed M17 design packages."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.application.gates import GateNotifier, HumanGateService
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
)
from syntra_build.domain import (
    DesignPackageId,
    DesignPackageStatus,
    GateId,
    GateState,
    GateType,
    Job,
    ProjectCreationContext,
    ProjectState,
    WorkerClass,
)
from syntra_build.domain.failures import FailureClassification, classify_failure
from syntra_build.infrastructure.logging import logging_context
from syntra_build.infrastructure.persistence import (
    SQLiteDesignPackageRepository,
    SQLiteHumanGateRepository,
    SQLiteProjectRepository,
    open_database,
)

_LOGGER = logging.getLogger(__name__)


class DesignApprovalNotificationExecutor:
    """Retry only notification, never generation, on a worker-owned connection."""

    def __init__(
        self,
        database_path: Path,
        notifier_factory: Callable[
            [sqlite3.Connection, ProjectCreationContext], GateNotifier
        ],
        *,
        failure_classifier: Callable[
            [BaseException], FailureClassification
        ] = classify_failure,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database_path = database_path
        self.notifier_factory = notifier_factory
        self.failure_classifier = failure_classifier
        self.clock = clock

    def execute(self, job: Job) -> JobExecutionResult:
        if (
            job.job_type != "DESIGN_APPROVAL_NOTIFY"
            or job.worker_class is not WorkerClass.MESSAGING
            or job.milestone_id is not None
            or set(job.payload or {}) != {"gate_id"}
        ):
            raise ValueError("design notification requires an exact project gate job")
        raw_gate = (job.payload or {}).get("gate_id")
        if not isinstance(raw_gate, str):
            raise ValueError("design notification requires a gate locator")
        gate_id = GateId.from_string(raw_gate)
        fields = {
            "project_id": str(job.project_id),
            "gate_id": str(gate_id),
            "job_id": str(job.id),
            "correlation_id": job.correlation_id,
        }
        with logging_context(**fields):
            _LOGGER.info(
                "Reconciling pending design approval notification",
                extra={
                    "event": "design_approval_notification_recovery",
                    **fields,
                    "metadata": {"attempt_number": job.attempt_number},
                },
            )
            try:
                with closing(open_database(self.database_path)) as connection:
                    gates = SQLiteHumanGateRepository(connection, lambda: str(uuid4()))
                    gate = gates.get(gate_id)
                    if (
                        gate.project_id != job.project_id
                        or gate.gate_type is not GateType.DESIGN_APPROVAL
                        or gate.milestone_id is not None
                        or not gate.artifact_reference
                        or not gate.artifact_reference.startswith("design-package:")
                    ):
                        raise ValueError("design approval gate identity conflicts")
                    package = SQLiteDesignPackageRepository(connection).get(
                        DesignPackageId.from_string(
                            gate.artifact_reference.removeprefix("design-package:")
                        )
                    )
                    if (
                        package.project_id != job.project_id
                        or package.approval_gate_id != gate.id
                    ):
                        raise ValueError("design approval package identity conflicts")
                    # A completed send/decision may precede Scheduler harvest.
                    if gate.state is not GateState.PENDING:
                        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)
                    projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
                    project = projects.get(job.project_id)
                    if project.state in {ProjectState.PAUSED, ProjectState.CANCELLED}:
                        return JobExecutionResult(JobExecutionDisposition.CANCELLED)
                    if (
                        project.state is not ProjectState.DESIGN_APPROVAL
                        or package.status is not DesignPackageStatus.PENDING_APPROVAL
                    ):
                        raise ValueError("design approval notification is not eligible")
                    context = projects.get_creation_context(job.project_id)
                    _LOGGER.info(
                        "Design approval notification starting",
                        extra={
                            "event": "design_approval_notification_started",
                            **fields,
                        },
                    )
                    HumanGateService(
                        gates,
                        response_id_factory=lambda: str(uuid4()),
                        authorised_responder_ids=frozenset(),
                    ).notify(
                        gate.id,
                        self.notifier_factory(connection, context),
                        occurred_at=self.clock(),
                    )
                    _LOGGER.info(
                        "Design approval gate notification confirmed",
                        extra={"event": "design_approval_gate_notified", **fields},
                    )
            except Exception as error:
                # Never log provider descriptions or message/document bodies.
                classification = self.failure_classifier(error)
                _LOGGER.warning(
                    "Design approval notification failed",
                    extra={
                        "event": "design_approval_notification_failed",
                        **fields,
                        "metadata": {
                            "failure_classification": classification.value,
                            "error_type": type(error).__name__,
                        },
                    },
                )
                return JobExecutionResult(
                    JobExecutionDisposition.FAILED,
                    error_id="design-approval-notification-failed",
                    failure_classification=classification,
                )
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)
