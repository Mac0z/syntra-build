# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from syntra_build.adapters.architect import (
    DESIGN_RESPONSE_SCHEMA,
    OpenAIArchitectProvider,
)
from syntra_build.application.architect import (
    ArchitectError,
    ArchitectFailureKind,
    build_design_request,
)
from syntra_build.domain import (
    ArchitectDesignMode,
    ArchitectDesignRequest,
    ArchitectDesignResponse,
    ArchitectProposedDecision,
    Project,
    ProjectDesignContext,
    ProjectId,
    ProjectState,
)
from syntra_build.domain.errors import DomainValidationError
from syntra_build.infrastructure.persistence import (
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)

PID = ProjectId.from_string("00000000-0000-0000-0000-000000000016")
NOW = datetime(2026, 9, 9, tzinfo=UTC)


def request() -> ArchitectDesignRequest:
    return build_design_request(
        ProjectDesignContext(
            Project(PID, "Demo", ProjectState.DESIGNING, NOW, NOW), None, (), (), ()
        ),
        "corr",
        ("platform?",),
    )


@pytest.mark.parametrize("mode", list(ArchitectDesignMode))
def test_all_design_modes_are_strictly_normalised(mode: ArchitectDesignMode) -> None:
    value: dict[str, object] = {
        "interface_version": "1.0",
        "correlation_id": "corr",
        "project_id": str(PID),
        "mode": mode.value,
        "message": "advice",
        "proposed_decisions": [],
        "open_questions": [],
    }
    assert ArchitectDesignResponse.from_dict(value).mode is mode


def test_unknown_mode_and_extra_fields_are_rejected() -> None:
    value: dict[str, object] = {
        "interface_version": "1.0",
        "correlation_id": "corr",
        "project_id": str(PID),
        "mode": "UNKNOWN",
        "message": "x",
        "proposed_decisions": [],
        "open_questions": [],
    }
    with pytest.raises(DomainValidationError):
        ArchitectDesignResponse.from_dict(value)
    value["extra"] = True
    with pytest.raises(DomainValidationError):
        ArchitectDesignResponse.from_dict(value)


def response_value(decisions: list[object]) -> dict[str, object]:
    return {
        "interface_version": "1.0",
        "correlation_id": "corr",
        "project_id": str(PID),
        "mode": "PROPOSE_DESIGN",
        "message": "Review these proposals.",
        "proposed_decisions": decisions,
        "open_questions": [],
    }


def proposed_decision(title: str = "Database") -> dict[str, object]:
    return {
        "decision_type": "DATABASE",
        "title": title,
        "proposal": "Use SQLite",
        "rationale": "It supports durable local state.",
    }


def test_proposed_decisions_round_trip_as_typed_advisory_values() -> None:
    raw = response_value([proposed_decision(), proposed_decision("Audit store")])
    response = ArchitectDesignResponse.from_dict(raw)

    assert response.to_dict() == raw
    assert len(response.proposed_decisions) == 2
    assert all(
        isinstance(item, ArchitectProposedDecision)
        for item in response.proposed_decisions
    )
    # The proposal contract is deliberately distinct from authoritative
    # ProjectDecision records and parsing it has no persistence dependency.
    assert response.proposed_decisions[0].proposal == "Use SQLite"


@pytest.mark.parametrize(
    "invalid",
    [
        {"title": "Database", "proposal": "SQLite", "rationale": "Local"},
        {
            "decision_type": "DATABASE",
            "title": "",
            "proposal": "SQLite",
            "rationale": "Local",
        },
        {
            "decision_type": "DATABASE",
            "title": "Database",
            "proposal": "SQLite",
            "rationale": "Local",
            "unknown": "not allowed",
        },
        "not-an-object",
    ],
    ids=["missing-field", "empty-field", "additional-field", "non-object"],
)
def test_invalid_proposed_decisions_are_rejected(invalid: object) -> None:
    with pytest.raises(DomainValidationError):
        ArchitectDesignResponse.from_dict(response_value([invalid]))


def test_openai_schema_strict_constrains_proposed_decisions() -> None:
    properties = cast(dict[str, Any], DESIGN_RESPONSE_SCHEMA["properties"])
    decision_schema = cast(
        dict[str, Any], cast(dict[str, Any], properties["proposed_decisions"])["items"]
    )
    assert decision_schema["additionalProperties"] is False
    assert set(decision_schema["required"]) == {
        "decision_type",
        "title",
        "proposal",
        "rationale",
    }
    assert set(decision_schema["properties"]) == set(decision_schema["required"])


def test_openai_uses_strict_schema_and_captures_usage() -> None:
    seen: dict[str, object] = {}

    def transport(
        payload: dict[str, object], key: str, timeout: float
    ) -> dict[str, object]:
        seen.update(payload)
        assert key == "secret" and timeout == 12
        return {
            "id": "resp_1",
            "status": "completed",
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"interface_version":"1.0","correlation_id":"corr","project_id":"00000000-0000-0000-0000-000000000016","mode":"ASK_USER","message":"Question","proposed_decisions":[],"open_questions":["platform"]}',
                        }
                    ]
                }
            ],
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens": 5,
                "output_details": {"reasoning_tokens": 2},
                "total_tokens": 15,
            },
        }

    provider = OpenAIArchitectProvider(
        api_key="secret", model="configured", timeout_seconds=12, transport=transport
    )
    assert provider.design(request()).mode is ArchitectDesignMode.ASK_USER
    assert cast(Any, seen["text"])["format"]["strict"] is True
    assert cast(Any, seen["text"])["format"]["schema"] == DESIGN_RESPONSE_SCHEMA
    assert provider.telemetry()["cached_input_tokens"] == 3
    assert "secret" not in str(seen)


def test_timeout_is_classified() -> None:
    def transport(
        payload: dict[str, object], key: str, timeout: float
    ) -> dict[str, object]:
        raise TimeoutError

    provider = OpenAIArchitectProvider(api_key="secret", model="m", transport=transport)
    with pytest.raises(ArchitectError) as caught:
        provider.design(request())
    assert caught.value.kind is ArchitectFailureKind.TIMEOUT


def test_parsing_proposals_does_not_materialise_project_decisions(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "advisory.db") as connection:
        apply_migrations(connection)
        SQLiteProjectRepository(connection, lambda: "unused").add(
            Project(PID, "Demo", ProjectState.DESIGNING, NOW, NOW)
        )
        before = connection.execute("SELECT count(*) FROM project_decisions").fetchone()
        response = ArchitectDesignResponse.from_dict(
            response_value([proposed_decision()])
        )
        after = connection.execute("SELECT count(*) FROM project_decisions").fetchone()

    assert response.proposed_decisions[0].proposal == "Use SQLite"
    assert before is not None and before[0] == 0
    assert after is not None and after[0] == 0
