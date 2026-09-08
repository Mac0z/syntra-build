"""M11 workflow-event domain validation."""

from datetime import UTC, datetime, timedelta

import pytest

from syntra_build.domain import (
    DomainValidationError,
    ProjectId,
    WorkflowEvent,
    WorkflowEventId,
    WorkflowEventSource,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
PROJECT = ProjectId.from_string("00000000-0000-0000-0000-000000000001")


def event(**changes: object) -> WorkflowEvent:
    values: dict[str, object] = {
        "id": WorkflowEventId.generate(),
        "project_id": PROJECT,
        "event_type": "TEST_EVENT",
        "occurred_at": NOW,
        "received_at": NOW + timedelta(seconds=1),
        "correlation_id": "correlation-1",
        "payload": {"nested": [{"ok": True}, None, 3.5]},
    }
    values.update(changes)
    return WorkflowEvent(**values)  # type: ignore[arg-type]


def test_valid_event_deeply_freezes_json_payload() -> None:
    payload: dict[str, object] = {"nested": [{"ok": True}]}
    created = event(payload=payload)
    assert created.payload is not None
    nested = created.payload["nested"]
    payload["nested"] = []
    assert nested == ({"ok": True},)
    with pytest.raises(TypeError):
        nested[0]["ok"] = False


@pytest.mark.parametrize(
    "changes",
    [
        {"occurred_at": NOW.replace(tzinfo=None)},
        {"received_at": NOW.replace(tzinfo=None)},
        {"received_at": NOW - timedelta(seconds=1)},
        {"source": "TELEGRAM"},
        {"correlation_id": " "},
        {"payload": {"bad": object()}},
        {"payload": {1: "bad"}},
        {"payload": {"bad": float("nan")}},
        {"project_id": WorkflowEventId.generate()},
    ],
)
def test_invalid_event_envelopes_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(DomainValidationError):
        event(**changes)


def test_external_source_requires_key_and_internal_source_does_not() -> None:
    assert event().external_deduplication_key is None
    external = event(
        source=WorkflowEventSource.TELEGRAM,
        external_deduplication_key="telegram:update:42",
    )
    assert external.source is WorkflowEventSource.TELEGRAM
    with pytest.raises(DomainValidationError, match="deduplication"):
        event(source=WorkflowEventSource.GITHUB)


def test_event_rejects_direct_self_causation() -> None:
    identity = WorkflowEventId.generate()
    with pytest.raises(DomainValidationError, match="cause itself"):
        event(id=identity, causation_event_id=identity)
