from pathlib import Path

import pytest

from syntra_build.application.workspaces import WorkspaceService, milestone_branch_name
from syntra_build.domain import MilestoneId, ProjectId
from syntra_build.domain.workspaces import WorkspaceError


def test_branch_name_is_deterministic_safe_and_bounded() -> None:
    assert milestone_branch_name(1, "CLI & Persistence Foundation!") == (
        "syntra/m01-cli-persistence-foundation"
    )
    assert milestone_branch_name(1, "x" * 500) == milestone_branch_name(1, "x" * 500)
    assert len(milestone_branch_name(123, "x" * 500)) <= 120


def test_managed_paths_are_derived_only_from_internal_ids(tmp_path: Path) -> None:
    service = WorkspaceService.__new__(WorkspaceService)
    service.data_root = tmp_path.resolve()
    service.repository_root = (tmp_path / "repositories").resolve()
    service.workspace_root = (tmp_path / "workspaces").resolve()
    project = ProjectId.generate()
    milestone = MilestoneId.generate()

    assert service._repository_path(project) == (
        tmp_path / "repositories" / str(project) / "repo.git"
    )
    assert service._workspace_path(project, milestone) == (
        tmp_path / "workspaces" / str(project) / str(milestone)
    )
    with pytest.raises(WorkspaceError, match="escapes"):
        service._contained(service.workspace_root, tmp_path / "outside")
