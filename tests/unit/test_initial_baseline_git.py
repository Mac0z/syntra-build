import subprocess
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch
from uuid import UUID

import pytest

from syntra_build.application.provisioning import ProvisioningError
from syntra_build.domain import ProjectId
from syntra_build.infrastructure.config import SecretValue
from syntra_build.infrastructure.git_initial import SubprocessInitialBaselineGit

USERNAME = "Mac0z"
PAT = "synthetic-github-pat-never-output"


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


def test_push_uses_ephemeral_askpass_credentials_and_clean_remote(
    tmp_path: Path,
) -> None:
    project_id = ProjectId(UUID(int=2))
    git = SubprocessInitialBaselineGit(
        tmp_path, github_username=USERNAME, github_token=SecretValue(PAT)
    )
    sha = git.create_commit(
        project_id, {"SPEC.md": b"spec", "AGENTS.md": b"agents"}, "initial"
    )
    remote_url = "https://github.com/Mac0z/example.git"
    original_run = subprocess.run
    askpass_path: Path | None = None

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal askpass_path
        if arguments[:2] != ["git", "push"]:
            return original_run(  # type: ignore[call-overload,no-any-return]
                arguments, **kwargs
            )
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        askpass_path = Path(environment["GIT_ASKPASS"])
        assert askpass_path.is_file()
        assert S_IMODE(askpass_path.stat().st_mode) == 0o700
        assert S_IMODE(askpass_path.parent.stat().st_mode) == 0o700
        assert askpass_path.parent.parent == tmp_path.resolve()
        assert askpass_path.parent != tmp_path / str(project_id)
        assert PAT not in " ".join(arguments)
        assert (
            original_run(
                [str(askpass_path), "Username for 'https://github.com':"],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == USERNAME
        )
        assert (
            original_run(
                [str(askpass_path), "Password for 'https://github.com':"],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == PAT
        )
        return subprocess.CompletedProcess(arguments, 0, "", "")

    with patch(
        "syntra_build.infrastructure.git_initial.subprocess.run", side_effect=run
    ):
        git.push_main(project_id, remote_url, sha)

    assert askpass_path is not None
    assert not askpass_path.exists()
    repository_path = tmp_path / str(project_id)
    assert (
        subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=repository_path, text=True
        ).strip()
        == remote_url
    )
    assert PAT not in (repository_path / ".git" / "config").read_text(encoding="utf-8")


def test_push_failure_removes_askpass_and_exposes_no_token(tmp_path: Path) -> None:
    project_id = ProjectId(UUID(int=3))
    git = SubprocessInitialBaselineGit(
        tmp_path, github_username=USERNAME, github_token=SecretValue(PAT)
    )
    sha = git.create_commit(
        project_id, {"SPEC.md": b"spec", "AGENTS.md": b"agents"}, "initial"
    )
    original_run = subprocess.run
    askpass_path: Path | None = None

    def fail_push(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal askpass_path
        if arguments[:2] != ["git", "push"]:
            return original_run(  # type: ignore[call-overload,no-any-return]
                arguments, **kwargs
            )
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        askpass_path = Path(environment["GIT_ASKPASS"])
        raise subprocess.CalledProcessError(128, arguments, stderr="push rejected")

    with (
        patch(
            "syntra_build.infrastructure.git_initial.subprocess.run",
            side_effect=fail_push,
        ),
        pytest.raises(ProvisioningError) as raised,
    ):
        git.push_main(project_id, "https://github.com/Mac0z/example.git", sha)

    assert askpass_path is not None
    assert not askpass_path.exists()
    assert PAT not in str(raised.value)
    assert PAT not in repr(raised.value)
    assert PAT not in repr(raised.value.__cause__)
