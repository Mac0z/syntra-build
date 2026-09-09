"""Provider-neutral orchestration for advisory Architect design calls."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast
from uuid import uuid4

from syntra_build.application.design import ProjectDesignContextService
from syntra_build.domain import (
    ARCHITECT_INTERFACE_VERSION,
    ArchitectDesignRequest,
    ArchitectDesignResponse,
    ProjectDesignContext,
    ProjectId,
)


class ArchitectFailureKind(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    CONFIGURATION = "CONFIGURATION"
    TIMEOUT = "TIMEOUT"
    TRANSIENT_PROVIDER = "TRANSIENT_PROVIDER"
    THROTTLED = "THROTTLED"
    PERMANENT_PROVIDER = "PERMANENT_PROVIDER"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    REFUSAL = "REFUSAL"
    INCOMPLETE_RESPONSE = "INCOMPLETE_RESPONSE"


class ArchitectError(RuntimeError):
    def __init__(self, kind: ArchitectFailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class ArchitectProvider(Protocol):
    provider_name: str
    model: str

    def design(self, request: ArchitectDesignRequest) -> ArchitectDesignResponse: ...
    def telemetry(self) -> dict[str, int | str | None]: ...


class ArchitectInteractionStore(Protocol):
    def begin(
        self,
        *,
        request_id: str,
        request: ArchitectDesignRequest,
        provider: str,
        model: str,
        reasoning_effort: str,
        created_at: datetime,
    ) -> None: ...
    def succeed(
        self,
        *,
        request_id: str,
        response: ArchitectDesignResponse,
        provider_response_id: str | None,
        usage: dict[str, int | str | None],
        completed_at: datetime,
    ) -> None: ...
    def fail(
        self, *, request_id: str, kind: ArchitectFailureKind, completed_at: datetime
    ) -> None: ...


def build_design_request(
    context: ProjectDesignContext,
    correlation_id: str,
    open_questions: tuple[str, ...] = (),
) -> ArchitectDesignRequest:
    initial = (
        context.creation_context.initial_request
        if context.creation_context
        else context.project.name
    )
    messages = cast(
        tuple[dict[str, object], ...],
        tuple(
            {
                "direction": m.direction.value,
                "text": m.text,
                "occurred_at": m.occurred_at.isoformat(),
                "correlation_id": m.correlation_id,
            }
            for m in context.messages
        ),
    )
    decisions = cast(
        tuple[dict[str, object], ...],
        tuple(
            {
                "decision_type": d.decision_type,
                "title": d.title,
                "decision": d.decision,
                "rationale": d.rationale,
                "source": d.source.value,
            }
            for d in context.decisions
        ),
    )
    documents = cast(
        tuple[dict[str, object], ...],
        tuple(
            {
                "document_type": d.document_type.value,
                "revision": d.revision,
                "status": d.status.value,
                "content": d.content,
                "content_hash": d.content_hash,
            }
            for d in context.documents
        ),
    )
    return ArchitectDesignRequest(
        ARCHITECT_INTERFACE_VERSION,
        correlation_id,
        context.project.id,
        context.project.name,
        initial,
        messages,
        decisions,
        open_questions,
        documents,
    )


class ArchitectDesignService:
    """Persist intent, call an untrusted worker, validate identity, persist outcome."""

    def __init__(
        self,
        context: ProjectDesignContextService,
        store: ArchitectInteractionStore,
        provider: ArchitectProvider,
        *,
        reasoning_effort: str = "high",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._context, self._store, self._provider = context, store, provider
        self._reasoning_effort, self._clock, self._id_factory = (
            reasoning_effort,
            clock,
            id_factory,
        )

    def design(
        self,
        project_id: ProjectId,
        correlation_id: str,
        open_questions: tuple[str, ...] = (),
    ) -> ArchitectDesignResponse:
        request = build_design_request(
            self._context.reconstruct(project_id), correlation_id, open_questions
        )
        request_id = self._id_factory()
        self._store.begin(
            request_id=request_id,
            request=request,
            provider=self._provider.provider_name,
            model=self._provider.model,
            reasoning_effort=self._reasoning_effort,
            created_at=self._clock(),
        )
        try:
            response = self._provider.design(request)
            if (
                response.project_id != request.project_id
                or response.correlation_id != request.correlation_id
                or response.interface_version != request.interface_version
            ):
                raise ArchitectError(
                    ArchitectFailureKind.MALFORMED_RESPONSE,
                    "Architect response identity does not match request",
                )
            usage = self._provider.telemetry()
            response_id = usage.get("provider_response_id")
            self._store.succeed(
                request_id=request_id,
                response=response,
                provider_response_id=response_id
                if isinstance(response_id, str)
                else None,
                usage=usage,
                completed_at=self._clock(),
            )
            return response
        except ArchitectError as error:
            self._store.fail(
                request_id=request_id, kind=error.kind, completed_at=self._clock()
            )
            raise
