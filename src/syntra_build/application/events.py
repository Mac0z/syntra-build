"""Deterministic, in-process dispatch of already-persisted workflow events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from syntra_build.domain.events import (
    EventProcessingStatus,
    PersistedWorkflowEvent,
    WorkflowEvent,
)
from syntra_build.domain.identifiers import WorkflowEventId
from syntra_build.infrastructure.persistence.errors import PersistenceError
from syntra_build.infrastructure.persistence.events import (
    SQLiteWorkflowEventRepository,
)


class EventHandlerOutcome(StrEnum):
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class EventProcessingResult:
    outcome: EventHandlerOutcome
    reason: str | None = None

    @classmethod
    def processed(cls) -> EventProcessingResult:
        return cls(EventHandlerOutcome.PROCESSED)

    @classmethod
    def rejected(cls, reason: str) -> EventProcessingResult:
        return cls(EventHandlerOutcome.REJECTED, reason)

    @classmethod
    def failed(cls, reason: str) -> EventProcessingResult:
        return cls(EventHandlerOutcome.FAILED, reason)


class WorkflowEventHandler(Protocol):
    def handle(self, event: WorkflowEvent) -> EventProcessingResult: ...


class _ReportedFailure(RuntimeError):
    pass


class WorkflowEventDispatcher:
    """Dispatch one event; looping, scheduling and backoff deliberately live later."""

    def __init__(
        self,
        repository: SQLiteWorkflowEventRepository,
        handlers: Mapping[str, WorkflowEventHandler],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        claim_token_factory: Callable[[], str] = lambda: str(uuid4()),
    ):
        if len(handlers) != len(set(handlers)):
            raise ValueError("each event type must have exactly one handler")
        self._repository = repository
        self._handlers = dict(handlers)
        self._clock = clock
        self._claim_tokens = claim_token_factory

    def dispatch(self, event_id: WorkflowEventId) -> PersistedWorkflowEvent:
        attempted_at = self._clock()
        claim_token = self._claim_tokens()
        try:
            with self._repository.processing_attempt(
                event_id, claim_token, attempted_at
            ) as record:
                handler = self._handlers.get(record.event.event_type)
                result = (
                    handler.handle(record.event)
                    if handler is not None
                    else EventProcessingResult.rejected(
                        f"unknown event type: {record.event.event_type}"
                    )
                )
                if not isinstance(result, EventProcessingResult):
                    raise TypeError(
                        "event handler returned an invalid processing result"
                    )
                if result.outcome is EventHandlerOutcome.FAILED:
                    raise _ReportedFailure(
                        result.reason or "event handler reported failure"
                    )
                status = (
                    EventProcessingStatus.PROCESSED
                    if result.outcome is EventHandlerOutcome.PROCESSED
                    else EventProcessingStatus.REJECTED
                )
                self._repository.finish_attempt(
                    event_id, claim_token, status, attempted_at, result.reason
                )
        except _ReportedFailure as error:
            return self._repository.record_failed_attempt(
                event_id, attempted_at, str(error)
            )
        except Exception as error:
            # Claim/terminal/persistence errors are caller-visible and must not be
            # rewritten as handler failures.
            if isinstance(error, PersistenceError):
                raise
            return self._repository.record_failed_attempt(
                event_id,
                attempted_at,
                f"{type(error).__name__}: {error}",
            )
        return self._repository.get(event_id)
