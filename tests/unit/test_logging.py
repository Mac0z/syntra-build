from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from enum import Enum
from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
    load_config,
)
from syntra_build.infrastructure.logging import (
    REDACTED,
    configure_logging,
    current_logging_context,
    logging_context,
    new_correlation_id,
)


def config(
    tmp_path: Path, *, level: str = "INFO", structured: bool = True
) -> ApplicationConfig:
    return load_config(
        {
            "filesystem": {
                "application_root": tmp_path / "application",
                "configuration_root": tmp_path / "configuration",
                "data_root": tmp_path / "data",
                "log_root": tmp_path / "logs",
            },
            "logging": {"level": level, "structured": structured},
        },
        environ={},
    )


def emitted(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_structured_event_contains_utc_timestamp_and_context(tmp_path: Path) -> None:
    stream = StringIO()
    configure_logging(config(tmp_path), stream=stream)
    logger = logging.getLogger("syntra_build.worker")

    with logging_context(correlation_id="corr-known", project_id="project-1"):
        logger.info("Project created", extra={"event": "project_created"})

    record = emitted(stream)[0]
    timestamp = datetime.fromisoformat(str(record["timestamp"]))
    assert timestamp.tzinfo == UTC
    assert record == record | {
        "level": "INFO",
        "logger": "syntra_build.worker",
        "component": "syntra_build.worker",
        "event": "project_created",
        "message": "Project created",
        "correlation_id": "corr-known",
        "project_id": "project-1",
    }


def test_context_is_scoped_nested_and_can_be_explicitly_cleared() -> None:
    assert current_logging_context() == {}
    with logging_context(correlation_id="outer", project_id="project"):
        assert current_logging_context()["correlation_id"] == "outer"
        with logging_context(correlation_id="inner", project_id=None, job_id="job"):
            assert dict(current_logging_context()) == {
                "correlation_id": "inner",
                "job_id": "job",
            }
        assert dict(current_logging_context()) == {
            "correlation_id": "outer",
            "project_id": "project",
        }
    assert current_logging_context() == {}


def test_async_tasks_have_isolated_contexts() -> None:
    async def operation(identifier: str) -> tuple[str, str]:
        with logging_context(correlation_id=identifier):
            await asyncio.sleep(0)
            return identifier, current_logging_context()["correlation_id"]

    async def run() -> list[tuple[str, str]]:
        return list(await asyncio.gather(operation("first"), operation("second")))

    assert asyncio.run(run()) == [("first", "first"), ("second", "second")]
    assert current_logging_context() == {}


def test_correlation_ids_are_uuid_values_and_explicit_ids_are_unchanged() -> None:
    generated = new_correlation_id()
    assert str(UUID(generated)) == generated
    with logging_context(correlation_id="caller-supplied"):
        assert current_logging_context()["correlation_id"] == "caller-supplied"
    assert "correlation_id" not in current_logging_context()


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
def test_sensitive_keys_are_redacted_from_final_output(
    tmp_path: Path, key: str
) -> None:
    secret = f"synthetic-{key}-value"
    stream = StringIO()
    configure_logging(config(tmp_path), stream=stream)

    logging.getLogger("syntra_build.security").info(
        "safe", extra={"event": "redaction_test", "metadata": {key: secret, "count": 2}}
    )

    output = stream.getvalue()
    assert secret not in output
    assert REDACTED in output
    assert emitted(stream)[0]["metadata"] == {key: REDACTED, "count": 2}


def test_secret_wrappers_and_supported_values_are_safe(tmp_path: Path) -> None:
    class Result(Enum):
        OK = "ok"

    secret = "synthetic-wrapper-value"
    stream = StringIO()
    configure_logging(config(tmp_path), stream=stream)
    logging.getLogger("syntra_build.safe").info(
        "values",
        extra={
            "event": "safe_values",
            "metadata": {
                "wrapped": SecretValue(secret),
                "inputs": SecretInputs(github_token=SecretValue(secret)),
                "path": Path("relative/path"),
                "when": datetime(2026, 1, 2, tzinfo=UTC),
                "result": Result.OK,
                "unknown": object(),
            },
        },
    )
    output = stream.getvalue()
    assert secret not in output
    metadata = emitted(stream)[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["wrapped"] == REDACTED
    assert metadata["inputs"] == REDACTED
    assert metadata["path"] == "relative/path"
    assert metadata["result"] == "ok"
    assert metadata["unknown"] == "[UNSERIALIZABLE:object]"


def test_exception_logging_has_identity_context_and_redacted_metadata(
    tmp_path: Path,
) -> None:
    secret = "synthetic-exception-token"
    stream = StringIO()
    configure_logging(config(tmp_path), stream=stream)
    logger = logging.getLogger("syntra_build.errors")

    with logging_context(correlation_id="error-correlation"):
        try:
            raise ValueError("useful failure")
        except ValueError:
            logger.exception(
                "Operation failed",
                extra={"event": "operation_failed", "metadata": {"token": secret}},
            )

    output = stream.getvalue()
    record = emitted(stream)[0]
    assert secret not in output
    assert record["event"] == "operation_failed"
    assert record["correlation_id"] == "error-correlation"
    assert record["error"] == {"type": "ValueError", "message": "useful failure"}
    assert "ValueError: useful failure" in str(record["exception"])


def test_level_is_honoured_and_reconfiguration_does_not_duplicate(
    tmp_path: Path,
) -> None:
    first = StringIO()
    second = StringIO()
    application_config = config(tmp_path, level="WARNING")
    configure_logging(application_config, stream=first)
    configure_logging(application_config, stream=second)
    logger = logging.getLogger("syntra_build.level")

    logger.info("filtered", extra={"event": "filtered"})
    logger.warning("once", extra={"event": "warning"})

    assert first.getvalue() == ""
    assert len(emitted(second)) == 1


def test_unstructured_configuration_uses_safe_text_output(tmp_path: Path) -> None:
    stream = StringIO()
    configure_logging(config(tmp_path, structured=False), stream=stream)
    logging.getLogger("syntra_build.text").info(
        "hello", extra={"event": "greeting", "metadata": {"password": "fake"}}
    )
    assert "greeting: hello" in stream.getvalue()
    assert "fake" not in stream.getvalue()
