"""Bounded metrics recording contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class APIProvider(StrEnum):
    GITHUB = "github"
    TELEGRAM = "telegram"
    ARCHITECT = "architect"


class APIFailureClassification(StrEnum):
    TRANSPORT = "transport"
    AUTHENTICATION = "authentication"
    REJECTION = "rejection"
    TRANSIENT = "transient"
    MALFORMED = "malformed"
    PROTOCOL = "protocol"


class MetricsRecorder(Protocol):
    def api_failure(
        self, provider: APIProvider, classification: APIFailureClassification
    ) -> None: ...

    def resource_guard_denied(self, reason: str) -> None: ...


class NoOpMetricsRecorder:
    def api_failure(
        self, provider: APIProvider, classification: APIFailureClassification
    ) -> None:
        return None

    def resource_guard_denied(self, reason: str) -> None:
        return None
