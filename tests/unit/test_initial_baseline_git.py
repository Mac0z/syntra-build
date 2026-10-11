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


def baseline_files() -> dict[str, bytes]:
    return {
        "SPEC.md": b"# Exact spec\n\n",
        "AGENTS.md": b"# Exact agents\r\n",
        ".github/workflows/ci.yml": b"name: Trusted CI\n\n",
    }


def run_git(path: Path, *arguments: str) -> bytes:
    environment = SubprocessInitialBaselineGit._environment()
    environment.update(
        GIT_AUTHOR_NAME="Syntra Build",
        GIT_AUTHOR_EMAIL="syntra@localhost",
        GIT_COMMITTER_NAME="Syntra Build",
        GIT_COMMITTER_EMAIL="syntra@localhost",
    )
    return subprocess.check_output(["git", *arguments], cwd=path, env=environment)


def test_initial_commit_contains_only_exact_approved_bytes(tmp_path: Path) -> None:
    project_id = ProjectId(UUID(int=1))
    git = SubprocessInitialBaselineGit(tmp_path)
    files = baseline_files()

    sha = git.create_commit(project_id, files, "Initial approved project design")
    path = tmp_path / str(project_id)

    assert len(sha) == 40
    for filename, content in files.items():
        assert run_git(path, "show", f"{sha}:{filename}") == content
    assert subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", sha], cwd=path, text=True
    ).splitlines() == sorted(files)
    assert git.create_commit(project_id, files, "Retry") == sha


def test_push_rejects_credential_bearing_remote(tmp_path: Path) -> None:
    project_id = ProjectId(UUID(int=1))
    git = SubprocessInitialBaselineGit(tmp_path)
    sha = git.create_commit(project_id, baseline_files(), "initial")

    with pytest.raises(ProvisioningError, match="credential-bearing"):
        git.push_main(project_id, "https://token@github.com/owner/repo.git", sha)


def test_push_uses_ephemeral_askpass_credentials_and_clean_remote(
    tmp_path: Path,
) -> None:
    project_id = ProjectId(UUID(int=2))
    git = SubprocessInitialBaselineGit(
        tmp_path, github_username=USERNAME, github_token=SecretValue(PAT)
    )
    sha = git.create_commit(project_id, baseline_files(), "initial")
    remote_url = "https://github.com/Mac0z/example.git"
    original_run = subprocess.run
    askpass_path: Path | None = None

    def run(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
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
        assert arguments == ["git", "push", "origin", f"{sha}:refs/heads/main"]
        assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert environment["GIT_CONFIG_VALUE_0"] == "/dev/null"
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
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

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
    sha = git.create_commit(project_id, baseline_files(), "initial")
    original_run = subprocess.run
    askpass_path: Path | None = None

    def fail_push(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
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


@pytest.mark.parametrize(
    "filename",
    [
        "../escaped",
        "/tmp/escaped",
        ".github/workflows/extra.yml",
        ".github/actions/run.yml",
    ],
)
def test_initial_commit_rejects_arbitrary_paths_before_writing(
    tmp_path: Path, filename: str
) -> None:
    project_id = ProjectId(UUID(int=4))
    files = baseline_files()
    files[filename] = b"untrusted"

    with pytest.raises(ProvisioningError, match="unexpected baseline files"):
        SubprocessInitialBaselineGit(tmp_path).create_commit(
            project_id, files, "initial"
        )

    assert not (tmp_path / str(project_id)).exists()


def test_initial_commit_rejects_old_document_only_baseline(tmp_path: Path) -> None:
    files = baseline_files()
    del files[".github/workflows/ci.yml"]
    with pytest.raises(ProvisioningError, match="unexpected baseline files"):
        SubprocessInitialBaselineGit(tmp_path).create_commit(
            ProjectId(UUID(int=4)), files, "initial"
        )


@pytest.mark.parametrize("filename", sorted(baseline_files()))
def test_replay_rejects_changed_bytes_without_rewriting(
    tmp_path: Path, filename: str
) -> None:
    project_id = ProjectId(UUID(int=5))
    files = baseline_files()
    git = SubprocessInitialBaselineGit(tmp_path)
    sha = git.create_commit(project_id, files, "initial")
    path = tmp_path / str(project_id)
    changed = files.copy()
    changed[filename] = changed[filename].rstrip(b"\n")

    with pytest.raises(ProvisioningError, match="existing baseline differs"):
        git.create_commit(project_id, changed, "retry")

    assert (path / filename).read_bytes() == files[filename]
    assert run_git(path, "rev-parse", "HEAD").strip().decode() == sha
    assert run_git(path, "status", "--porcelain") == b""


@pytest.mark.parametrize("mode", ["100755", "120000", "160000"])
def test_replay_rejects_nonregular_committed_workflow(
    tmp_path: Path, mode: str
) -> None:
    project_id = ProjectId(UUID(int=6))
    git = SubprocessInitialBaselineGit(tmp_path)
    files = baseline_files()
    sha = git.create_commit(project_id, files, "initial")
    path = tmp_path / str(project_id)
    filename = ".github/workflows/ci.yml"
    object_id = (
        sha.encode()
        if mode == "160000"
        else run_git(path, "rev-parse", f"HEAD:{filename}").strip()
    )
    run_git(
        path, "update-index", "--cacheinfo", f"{mode},{object_id.decode()},{filename}"
    )
    run_git(path, "commit", "--amend", "--no-edit")

    with pytest.raises(ProvisioningError, match="unexpected baseline tree"):
        git.create_commit(project_id, files, "retry")


def test_replay_rejects_extra_committed_tree_even_if_worktree_hides_it(
    tmp_path: Path,
) -> None:
    project_id = ProjectId(UUID(int=7))
    git = SubprocessInitialBaselineGit(tmp_path)
    files = baseline_files()
    git.create_commit(project_id, files, "initial")
    path = tmp_path / str(project_id)
    blob = run_git(path, "rev-parse", "HEAD:SPEC.md").strip().decode()
    run_git(
        path,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob},.github/workflows/evil.yml",
    )
    run_git(path, "commit", "--amend", "--no-edit")

    with pytest.raises(ProvisioningError, match="unexpected baseline tree"):
        git.create_commit(project_id, files, "retry")


@pytest.mark.parametrize("mutation", ["history", "branch", "reference"])
def test_replay_rejects_substituted_history_or_references(
    tmp_path: Path, mutation: str
) -> None:
    project_id = ProjectId(UUID(int=8))
    git = SubprocessInitialBaselineGit(tmp_path)
    files = baseline_files()
    git.create_commit(project_id, files, "initial")
    path = tmp_path / str(project_id)
    if mutation == "history":
        run_git(path, "commit", "--allow-empty", "-m", "foreign history")
    elif mutation == "branch":
        run_git(path, "checkout", "-b", "foreign")
    else:
        run_git(path, "tag", "foreign")

    with pytest.raises(ProvisioningError, match="unexpected baseline"):
        git.create_commit(project_id, files, "retry")


@pytest.mark.parametrize("mutation", ["workflow", "hook", "configuration", "symlink"])
def test_push_rejects_hostile_local_state_before_credentials(
    tmp_path: Path, mutation: str
) -> None:
    project_id = ProjectId(UUID(int=9))
    git = SubprocessInitialBaselineGit(tmp_path)
    sha = git.create_commit(project_id, baseline_files(), "initial")
    path = tmp_path / str(project_id)
    if mutation == "workflow":
        (path / ".github/workflows/evil.yml").write_bytes(b"evil")
        (path / ".git/info").mkdir(exist_ok=True)
        (path / ".git/info/exclude").write_text("evil.yml\n")
    elif mutation == "hook":
        (path / ".git/hooks").mkdir(exist_ok=True)
        (path / ".git/hooks/pre-push").write_text("#!/bin/sh\nexit 0\n")
    elif mutation == "configuration":
        run_git(path, "config", "credential.helper", "!echo untrusted")
    else:
        (path / ".github/workflows/evil.yml").symlink_to(tmp_path / "external")

    with (
        patch.object(git, "_push_authentication_environment") as authentication,
        pytest.raises(ProvisioningError, match="baseline"),
    ):
        git.push_main(project_id, "https://github.com/Mac0z/example.git", sha)
    authentication.assert_not_called()


@pytest.mark.parametrize(
    "target", ["project", "workflow_directory", "workflow_file", "git_directory"]
)
def test_create_rejects_symlink_escapes(tmp_path: Path, target: str) -> None:
    project_id = ProjectId(UUID(int=10))
    git = SubprocessInitialBaselineGit(tmp_path)
    files = baseline_files()
    path = tmp_path / str(project_id)
    outside = tmp_path / "external"
    outside.mkdir()
    if target == "project":
        path.symlink_to(outside, target_is_directory=True)
    else:
        git.create_commit(project_id, files, "initial")
        if target == "workflow_directory":
            workflow = path / ".github/workflows/ci.yml"
            workflow.unlink()
            workflow.parent.rmdir()
            workflow.parent.symlink_to(outside, target_is_directory=True)
        elif target == "workflow_file":
            workflow = path / ".github/workflows/ci.yml"
            workflow.unlink()
            workflow.symlink_to(outside / "ci.yml")
        else:
            (path / ".git").rename(outside / "git")
            (path / ".git").symlink_to(outside / "git", target_is_directory=True)

    with pytest.raises(ProvisioningError, match="path|symlink|Git directory"):
        git.create_commit(project_id, files, "retry")
    assert not (outside / "ci.yml").exists()


def test_ambient_git_configuration_cannot_redirect_or_execute_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = ProjectId(UUID(int=11))
    hooks = tmp_path / "global-hooks"
    hooks.mkdir()
    marker = tmp_path / "executed"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o700)
    config = tmp_path / "global-config"
    config.write_text(f"[core]\n\thooksPath = {hooks}\n\tautocrlf = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "foreign"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    files = baseline_files()
    git = SubprocessInitialBaselineGit(tmp_path)

    sha = git.create_commit(project_id, files, "initial")

    assert not marker.exists()
    path = tmp_path / str(project_id)
    for filename, content in files.items():
        assert run_git(path, "show", f"{sha}:{filename}") == content


def test_replay_recovers_exact_staged_baseline_before_commit(tmp_path: Path) -> None:
    project_id = ProjectId(UUID(int=12))
    git = SubprocessInitialBaselineGit(tmp_path)
    path = tmp_path / str(project_id)
    path.mkdir()
    run_git(path, "init", "--initial-branch=main", "--template=")
    files = baseline_files()
    for filename, content in files.items():
        target = path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    run_git(path, "add", "--", *files)

    sha = git.create_commit(project_id, files, "initial")

    assert git.create_commit(project_id, files, "retry") == sha


def test_replay_adopts_packed_initial_reference(tmp_path: Path) -> None:
    project_id = ProjectId(UUID(int=13))
    git = SubprocessInitialBaselineGit(tmp_path)
    files = baseline_files()
    sha = git.create_commit(project_id, files, "initial")
    run_git(tmp_path / str(project_id), "pack-refs", "--all")
    assert git.create_commit(project_id, files, "retry") == sha


def test_create_rejects_dangling_git_directory_symlink_before_init(
    tmp_path: Path,
) -> None:
    project_id = ProjectId(UUID(int=14))
    path = tmp_path / str(project_id)
    path.mkdir()
    target = tmp_path / "external.git"
    (path / ".git").symlink_to(target, target_is_directory=True)

    with pytest.raises(ProvisioningError, match="Git directory"):
        SubprocessInitialBaselineGit(tmp_path).create_commit(
            project_id, baseline_files(), "initial"
        )

    assert not target.exists()


def test_replay_rejects_hidden_empty_tree(tmp_path: Path) -> None:
    project_id = ProjectId(UUID(int=15))
    git = SubprocessInitialBaselineGit(tmp_path)
    files = baseline_files()
    git.create_commit(project_id, files, "initial")
    path = tmp_path / str(project_id)
    empty = subprocess.check_output(
        ["git", "mktree"], input=b"", cwd=path, env=git._environment()
    ).strip()
    tree = (
        subprocess.check_output(
            ["git", "mktree"],
            input=run_git(path, "ls-tree", "HEAD")
            + b"040000 tree "
            + empty
            + b"\tunused\n",
            cwd=path,
            env=git._environment(),
        )
        .strip()
        .decode()
    )
    commit = run_git(path, "commit-tree", tree, "-m", "untrusted tree").strip().decode()
    run_git(path, "update-ref", "refs/heads/main", commit)

    with pytest.raises(ProvisioningError, match="unexpected baseline tree"):
        git.create_commit(project_id, files, "retry")
