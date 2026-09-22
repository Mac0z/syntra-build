import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.application.provisioning import ProvisioningError
from syntra_build.domain import ProjectId
from syntra_build.infrastructure.git_initial import SubprocessInitialBaselineGit


def test_initial_commit_contains_only_exact_approved_bytes(tmp_path: Path) -> None:
    project_id = ProjectId(UUID(int=1))
    git = SubprocessInitialBaselineGit(tmp_path)
    files = {"SPEC.md": b"# Exact spec\n", "AGENTS.md": b"# Exact agents\n"}

    sha = git.create_commit(project_id, files, "Initial approved project design")
    path = tmp_path / str(project_id)

    assert len(sha) == 40
    assert (
        subprocess.check_output(["git", "show", f"{sha}:SPEC.md"], cwd=path)
        == files["SPEC.md"]
    )
    assert (
        subprocess.check_output(["git", "show", f"{sha}:AGENTS.md"], cwd=path)
        == files["AGENTS.md"]
    )
    assert subprocess.check_output(
        ["git", "ls-tree", "--name-only", sha], cwd=path, text=True
    ).splitlines() == ["AGENTS.md", "SPEC.md"]


def test_push_rejects_credential_bearing_remote(tmp_path: Path) -> None:
    project_id = ProjectId(UUID(int=1))
    git = SubprocessInitialBaselineGit(tmp_path)
    sha = git.create_commit(
        project_id, {"SPEC.md": b"spec", "AGENTS.md": b"agents"}, "initial"
    )

    with pytest.raises(ProvisioningError, match="credential-bearing"):
        git.push_main(project_id, "https://token@github.com/owner/repo.git", sha)
