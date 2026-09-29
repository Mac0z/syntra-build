from uuid import uuid4

import pytest

from syntra_build.domain import (
    ArchitectTask,
    ArchitectTaskType,
    DomainValidationError,
    MilestoneId,
    ProjectId,
)


def payload() -> dict[str, object]:
    return {
        "interface_version": "1.0",
        "correlation_id": "corr",
        "project_id": str(uuid4()),
        "milestone_id": str(uuid4()),
        "task_type": "IMPLEMENT",
        "objective": "Implement the approved milestone.",
        "requirements": ["Keep state durable"],
        "acceptance_criteria": ["Tests pass"],
        "constraints": [],
        "tests_required": ["pytest"],
        "explicit_exclusions": ["No merge"],
    }


def test_architect_task_is_strict_and_typed() -> None:
    task = ArchitectTask.from_dict(payload())
    assert isinstance(task.project_id, ProjectId)
    assert isinstance(task.milestone_id, MilestoneId)
    assert task.task_type is ArchitectTaskType.IMPLEMENT
    assert ArchitectTask.from_dict(task.to_dict()) == task


@pytest.mark.parametrize("change", [{"unknown": True}, {"task_type": "OTHER"}])
def test_architect_task_rejects_unknown_fields_and_vocabulary(
    change: dict[str, object],
) -> None:
    value = payload() | change
    with pytest.raises(DomainValidationError):
        ArchitectTask.from_dict(value)
