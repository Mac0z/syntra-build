from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from enum import Enum
from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.infrastructure.config import LoggingConfig, SecretInputs, SecretValue
from syntra_build.infrastructure.logging import (
    REDACTED,
    clear_logging_context,
    configure_logging,
    get_logging_context,
    logging_context,
    new_correlation_id,
)


def emitted_records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_structured_record_has_stable_fields_and_utc_timestamp() -> None:
    stream = StringIO()
    logger = configure_logging(LoggingConfig(), stream=stream)

    with logging_context(correlation_id="corr-known", project_id="proj-1"):
        logger.info("project_created", extra={"component": "orchestrator"})

    record = emitted_records(stream)[0]
    timestamp = str(record["timestamp"])
    assert datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo == UTC
    assert record == {
        "component": "orchestrator",
        "correlation_id": "corr-known",
        "event": "project_created",
        "level": "INFO",
        "logger": "syntra_build",
        "message": "project_created",
        "project_id": "proj-1",
        "timestamp": timestamp,
    }


def test_context_scopes_nest_restore_and_clear() -> None:
    assert get_logging_context() == {}
    with logging_context(correlation_id="outer", project_id="project"):
        assert get_logging_context() == {
            "correlation_id": "outer",
            "project_id": "project",
        }
        with logging_context(correlation_id="inner", job_id="job"):
            assert get_logging_context() == {
                "correlation_id": "inner",
                "project_id": "project",
                "job_id": "job",
            }
        assert get_logging_context()["correlation_id"] == "outer"
        with clear_logging_context():
            assert get_logging_context() == {}
        assert get_logging_context()["project_id"] == "project"
    assert get_logging_context() == {}


def test_context_survives_await_and_isolated_async_tasks_do_not_leak() -> None:
    async def observe(identifier: str) -> tuple[str | None, str | None]:
        with logging_context(correlation_id=identifier):
            before = get_logging_context().get("correlation_id")
            await asyncio.sleep(0)
            return before, get_logging_context().get("correlation_id")

    async def run() -> list[tuple[str | None, str | None]]:
        return list(await asyncio.gather(observe("first"), observe("second")))

    assert asyncio.run(run()) == [("first", "first"), ("second", "second")]
    assert get_logging_context() == {}


def test_correlation_id_is_uuid_and_absence_does_not_generate_one() -> None:
    assert UUID(new_correlation_id()).version == 4
    with logging_context(correlation_id="supplied"):
        assert get_logging_context()["correlation_id"] == "supplied"
    with logging_context(project_id="project-only"):
        assert "correlation_id" not in get_logging_context()


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "GitHub_Token",
        "API_KEY",
        "Authorization",
        "password",
        "private_key",
        "credential",
    ],
)
def test_sensitive_metadata_keys_are_redacted_from_final_output(key: str) -> None:
    synthetic_secret = f"synthetic-{key}-value"
    stream = StringIO()
    logger = configure_logging(LoggingConfig(), stream=stream)

    logger.info(
        "redaction_checked",
        extra={"metadata": {key: synthetic_secret, "ordinary": "visible"}},
    )

    output = stream.getvalue()
    record = emitted_records(stream)[0]
    assert synthetic_secret not in output
    assert record["metadata"] == {key: REDACTED, "ordinary": "visible"}


def test_m1_secret_wrappers_are_never_unwrapped() -> None:
    synthetic_secret = "synthetic-secret-wrapper-value"
    secret = SecretValue(synthetic_secret)
    secret_inputs = SecretInputs(github_token=secret)
    stream = StringIO()
    logger = configure_logging(LoggingConfig(), stream=stream)

    logger.info("diagnostic", extra={"value": secret, "inputs": secret_inputs})

    assert synthetic_secret not in stream.getvalue()
    record = emitted_records(stream)[0]
    assert record["value"] == REDACTED
    assert record["inputs"] == REDACTED


def test_sensitive_mapping_used_as_message_arguments_is_redacted() -> None:
    stream = StringIO()
    logger = configure_logging(LoggingConfig(), stream=stream)

    logger.info(
        "authorization=%(Authorization)s ordinary=%(ordinary)s",
        {"Authorization": "synthetic-bearer", "ordinary": "visible"},
    )

    output = stream.getvalue()
    assert "synthetic-bearer" not in output
    assert f"authorization={REDACTED} ordinary=visible" in output


def test_exception_record_retains_event_context_and_safe_metadata() -> None:
    stream = StringIO()
    logger = configure_logging(LoggingConfig(), stream=stream)

    with logging_context(correlation_id="failure-correlation"):
        try:
            raise ValueError("useful failure")
        except ValueError:
            logger.exception(
                "operation_failed",
                extra={"metadata": {"Authorization": "synthetic-bearer"}},
            )

    output = stream.getvalue()
    record = emitted_records(stream)[0]
    assert "synthetic-bearer" not in output
    assert record["correlation_id"] == "failure-correlation"
    assert record["event"] == "operation_failed"
    error = record["error"]
    assert isinstance(error, dict)
    assert error["type"] == "ValueError"
    assert error["message"] == "useful failure"
    assert "Traceback" in str(error["stack_trace"])


def test_configured_level_is_honoured() -> None:
    stream = StringIO()
    logger = configure_logging(LoggingConfig(level="WARNING"), stream=stream)

    logger.info("ignored")
    logger.warning("retained")

    assert [record["event"] for record in emitted_records(stream)] == ["retained"]


def test_reconfiguration_replaces_handler_without_duplicate_records() -> None:
    first = StringIO()
    second = StringIO()
    logger = configure_logging(LoggingConfig(), stream=first)
    logger = configure_logging(LoggingConfig(), stream=second)

    logger.info("once")

    assert first.getvalue() == ""
    assert len(emitted_records(second)) == 1


def test_known_safe_values_convert_and_unknown_objects_fail_closed() -> None:
    class State(Enum):
        READY = "ready"

    stream = StringIO()
    logger = configure_logging(LoggingConfig(), stream=stream)
    logger.info(
        "converted",
        extra={
            "metadata": {
                "path": Path("relative/path"),
                "when": datetime(2026, 1, 2, tzinfo=UTC),
                "state": State.READY,
                "unknown": object(),
            }
        },
    )

    metadata = emitted_records(stream)[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata == {
        "path": "relative/path",
        "state": "ready",
        "unknown": "[UNSUPPORTED:object]",
        "when": "2026-01-02T00:00:00Z",
    }


def test_unstructured_development_output_remains_secret_safe() -> None:
    stream = StringIO()
    logger = configure_logging(LoggingConfig(structured=False), stream=stream)
    logger.info("event", extra={"metadata": {"token": "synthetic-token"}})

    assert "synthetic-token" not in stream.getvalue()
    assert "INFO syntra_build event" in stream.getvalue()
