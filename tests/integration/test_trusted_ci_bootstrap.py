"""Offline provisioning, real Git and SQLite trust-boundary acceptance tests."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.application.change_validation import ChangeValidationService
from syntra_build.application.ci_profiles import (
    TRUSTED_CI_WORKFLOW_PATH,
    select_ci_profile,
)
from syntra_build.application.provisioning import (
    AmbiguousPushResult,
    ProvisioningError,
    ProvisioningFailure,
    RemoteRepository,
    RepositoryProvisioningService,
)
from syntra_build.application.security import SecurityPolicy
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain import (
    MilestoneId,
    ProjectId,
    ProjectState,
    RepositoryVisibility,
)
from syntra_build.domain.change_validation import FindingCode, ValidationDecision
from syntra_build.domain.provisioning import RepositoryCIIntent
from syntra_build.domain.workspaces import WorkspaceError
from syntra_build.infrastructure.git_initial import SubprocessInitialBaselineGit
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import (
    SQLiteProjectRepository,
    SQLiteProvisioningRepository,
    apply_migrations,
    open_database,
)

NOW = datetime(2026, 10, 11, tzinfo=UTC)
PID = ProjectId(UUID(int=730))
MID = MilestoneId(UUID(int=731))
SPEC_ID = str(UUID(int=732))
AGENTS_ID = str(UUID(int=733))
PACKAGE_ID = str(UUID(int=734))
GATE_ID = str(UUID(int=735))
REMOTE_URL = "https://github.com/fixture/generated-python.git"
SPEC = """# Approved Python application

Build an application with a tested addition operation.

```syntra-ci
profile = "python-pytest-v1"
python-version = "3.12"
```
"""
AGENTS = "# Approved rules\nImplementation workers must preserve trusted CI.\n"


class SimulatedProcessLoss(RuntimeError):
    """Interrupt between durable intent and the recorded side-effect outcome."""


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _seed_approval(connection: sqlite3.Connection) -> None:
    timestamp = NOW.isoformat()
    connection.execute(
        """INSERT INTO projects
        (id,name,state,created_at,updated_at,last_state_change_at,
         canonical_name,repository_visibility)
        VALUES (?,?,'PROVISIONING',?,?,?,'generated-python','public')""",
        (str(PID), "Generated Python", timestamp, timestamp, timestamp),
    )
    for document_id, kind, content in (
        (SPEC_ID, "SPEC", SPEC),
        (AGENTS_ID, "AGENTS", AGENTS),
    ):
        connection.execute(
            """INSERT INTO project_documents
            (id,project_id,document_type,revision,status,content,content_hash,
             created_at,created_by)
            VALUES (?,?,?,1,'DRAFT',?,?,?,'architect')""",
            (
                document_id,
                str(PID),
                kind,
                content,
                sha256(content.encode()).hexdigest(),
                timestamp,
            ),
        )
    connection.execute(
        """INSERT INTO architect_requests
        (id,project_id,request_type,provider,model,reasoning_level,
         request_schema_version,request_payload_json,correlation_id,
         started_at,completed_at,status)
        VALUES ('fixture-design',?,'SPECIFICATION_DRAFT','offline','offline','high',
                '1','{}','approval',?,?,'SUCCEEDED')""",
        (str(PID), timestamp, timestamp),
    )
    connection.execute(
        """INSERT INTO human_gates
        (id,project_id,gate_type,state,title,prompt,expected_response_type,
         options_json,created_at,created_by,correlation_id)
        VALUES (?,?,'DESIGN_APPROVAL','RESOLVED','Approve','Approve?',
                'DESIGN_APPROVAL','[]',?,'system','approval')""",
        (GATE_ID, str(PID), timestamp),
    )
    connection.execute(
        """INSERT INTO design_packages
        (id,project_id,architect_request_id,spec_document_id,agents_document_id,
         repository_visibility,design_summary,planned_milestones_json,
         assumptions_json,non_blocking_issues_json,status,approval_gate_id,created_at)
        VALUES (?,?,'fixture-design',?,?,'public','Python application','[]','[]','[]',
                'PENDING_APPROVAL',?,?)""",
        (PACKAGE_ID, str(PID), SPEC_ID, AGENTS_ID, GATE_ID, timestamp),
    )
    connection.execute(
        """UPDATE project_documents SET status='APPROVED',approved_at=?,
        approved_by='human' WHERE project_id=?""",
        (timestamp, str(PID)),
    )
    connection.execute(
        """UPDATE design_packages SET status='APPROVED',approved_at=?,
        approved_by='human' WHERE id=?""",
        (timestamp, PACKAGE_ID),
    )
    connection.commit()


def _durable_ci(database_path: Path) -> RepositoryCIIntent:
    """A separate database connection proves the intent is already committed."""
    observer = open_database(database_path)
    try:
        intent = SQLiteProvisioningRepository(observer).ci_intent(PID)
        assert intent is not None
        assert (
            intent.workflow_content.encode()
            == select_ci_profile(SPEC).workflow_content()
        )
        assert str(intent.spec_document_id) == SPEC_ID
        assert intent.spec_content_hash == sha256(SPEC.encode()).hexdigest()
        assert intent.agents_content_hash == sha256(AGENTS.encode()).hexdigest()
        assert intent.default_branch == "main"
        return intent
    finally:
        observer.close()


class LocalGitHub:
    """Provider-neutral gateway backed solely by a temporary bare Git repository."""

    def __init__(self, root: Path) -> None:
        self.database_path = root / "state.db"
        self.remote_path = root / "remote.git"
        self.create_calls = 0
        self.configure_calls = 0
        self.hide_after_configuration = False
        self.sha_override: str | None = None
        self.file_overrides: dict[str, bytes | None] = {}
        self.reads: list[tuple[str, str]] = []

    def get_repository(self, owner: str, name: str) -> RemoteRepository | None:
        assert (owner, name) == ("fixture", "generated-python")
        if not self.remote_path.exists() or (
            self.hide_after_configuration and self.configure_calls
        ):
            return None
        return RemoteRepository(
            730, owner, name, f"{owner}/{name}", RepositoryVisibility.PUBLIC, "main"
        )

    def create_repository(
        self, name: str, visibility: RepositoryVisibility
    ) -> RemoteRepository:
        _durable_ci(self.database_path)
        assert name == "generated-python" and visibility is RepositoryVisibility.PUBLIC
        self.create_calls += 1
        self.remote_path.mkdir()
        _git(self.remote_path, "init", "--bare", "--initial-branch=main")
        remote = self.get_repository("fixture", name)
        assert remote is not None
        return remote

    def main_sha(self, owner: str, name: str) -> str | None:
        assert (owner, name) == ("fixture", "generated-python")
        if not self.remote_path.exists():
            return None
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "refs/heads/main"],
            cwd=self.remote_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            return None
        return self.sha_override or result.stdout.strip()

    def configure_repository(self, owner: str, name: str) -> None:
        assert (owner, name) == ("fixture", "generated-python")
        self.configure_calls += 1

    def file_content(self, owner: str, name: str, sha: str, path: str) -> bytes | None:
        assert (owner, name) == ("fixture", "generated-python")
        self.reads.append((sha, path))
        if path in self.file_overrides:
            return self.file_overrides[path]
        result = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            cwd=self.remote_path,
            check=False,
            capture_output=True,
            timeout=30,
        )
        return result.stdout if result.returncode == 0 else None


class OfflineBaselineGit(SubprocessInitialBaselineGit):
    """Real baseline commits; local transport replaces the authenticated provider."""

    def __init__(self, root: Path, github: LocalGitHub) -> None:
        super().__init__(root / "baselines")
        self.github = github
        self.commit_calls = 0
        self.push_calls = 0
        self.ambiguous_push = False
        self.lose_after_commit = False
        self.lose_before_push = False

    def create_commit(
        self, project_id: ProjectId, files: dict[str, bytes], message: str
    ) -> str:
        self.commit_calls += 1
        intent = _durable_ci(self.github.database_path)
        assert files == {
            "SPEC.md": SPEC.encode(),
            "AGENTS.md": AGENTS.encode(),
            intent.workflow_path: intent.workflow_content.encode(),
        }
        result = super().create_commit(project_id, files, message)
        if self.lose_after_commit:
            raise SimulatedProcessLoss("lost after the local Git commit")
        return result

    @contextmanager
    def _push_authentication_environment(self) -> Iterator[dict[str, str]]:
        yield {"SYNTRA_TEST_BARE_REMOTE": str(self.github.remote_path)}

    @classmethod
    def _run_bytes(
        cls, arguments: list[str], path: Path, env: Mapping[str, str] | None = None
    ) -> bytes:
        if arguments[:3] == ["git", "push", "origin"]:
            assert env is not None
            # Only final transport is replaced. Production push_main still checks
            # local repository identity, history, exact SHA/tree and origin URL.
            _git(path, "push", env["SYNTRA_TEST_BARE_REMOTE"], *arguments[3:])
            return b""
        return super()._run_bytes(arguments, path, env)

    def push_main(
        self, project_id: ProjectId, remote_url: str, expected_sha: str
    ) -> None:
        self.push_calls += 1
        assert remote_url == REMOTE_URL
        observer = open_database(self.github.database_path)
        try:
            baseline = SQLiteProvisioningRepository(observer).baseline(project_id)
            assert baseline is not None and baseline.commit_sha == expected_sha
        finally:
            observer.close()
        if self.lose_before_push:
            raise SimulatedProcessLoss("lost before the remote Git push")
        super().push_main(project_id, remote_url, expected_sha)
        if self.ambiguous_push:
            raise AmbiguousPushResult("offline transport lost its acknowledgement")


class OfflineWorkspaceGit(TrustedGit):
    """Keep normal canonical identities while redirecting only network transport."""

    def __init__(self, root: Path, remote_path: Path) -> None:
        super().__init__(root / "auth")
        self.remote_path = remote_path

    @contextmanager
    def _auth(self, remote_url: str) -> Iterator[dict[str, str]]:
        assert remote_url == REMOTE_URL
        yield dict(
            os.environ,
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0=f"url.{self.remote_path}.insteadOf",
            GIT_CONFIG_VALUE_0=REMOTE_URL,
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
        )


@pytest.fixture
def database(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_database(tmp_path / "state.db")
    apply_migrations(connection)
    _seed_approval(connection)
    yield connection
    connection.close()


def _service(
    connection: sqlite3.Connection, root: Path
) -> tuple[RepositoryProvisioningService, LocalGitHub, OfflineBaselineGit]:
    github = LocalGitHub(root)
    git = OfflineBaselineGit(root, github)
    return (
        RepositoryProvisioningService(connection, github, git, owner="fixture"),
        github,
        git,
    )


def _workspace(
    connection: sqlite3.Connection, root: Path, github: LocalGitHub
) -> tuple[Path, WorkspaceService, ChangeValidationService]:
    connection.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,created_at,updated_at)
        VALUES (?,?,0,'M0','First Python application','READY',?,?)""",
        (str(MID), str(PID), NOW.isoformat(), NOW.isoformat()),
    )
    connection.commit()
    trusted = OfflineWorkspaceGit(root, github.remote_path)
    workspaces = WorkspaceService(connection, trusted, root / "data")
    workspace = workspaces.prepare_workspace(PID, MID, NOW)
    validation = ChangeValidationService(connection, trusted, root / "data")
    return workspace.path, workspaces, validation


def _assert_not_ready(connection: sqlite3.Connection) -> None:
    assert (
        SQLiteProjectRepository(connection, lambda: "unused").get(PID).state
        is ProjectState.PROVISIONING
    )
    baseline = SQLiteProvisioningRepository(connection).baseline(PID)
    assert baseline is not None and baseline.verified_at is None
    assert (
        connection.execute(
            """SELECT count(*) FROM state_transitions
        WHERE project_id=? AND new_state='READY'""",
            (str(PID),),
        ).fetchone()[0]
        == 0
    )


def test_real_baseline_and_first_application_branch_keep_trusted_ci(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    service, github, initial_git = _service(database, tmp_path)
    result = service.provision(PID, NOW, "bootstrap")
    intent = _durable_ci(tmp_path / "state.db")
    assert result.verified and result.project_state is ProjectState.READY
    assert (github.create_calls, initial_git.commit_calls, initial_git.push_calls) == (
        1,
        1,
        1,
    )
    assert _git(
        github.remote_path, "ls-tree", "-r", "--name-only", result.baseline_commit_sha
    ).splitlines() == [TRUSTED_CI_WORKFLOW_PATH, "AGENTS.md", "SPEC.md"]
    assert (
        github.file_content(
            "fixture",
            "generated-python",
            result.baseline_commit_sha,
            TRUSTED_CI_WORKFLOW_PATH,
        )
        == intent.workflow_content.encode()
    )

    worktree, workspaces, validation = _workspace(database, tmp_path, github)
    (worktree / "pyproject.toml").write_text(
        '[project]\nname = "fixture-application"\nversion = "0.1.0"\n'
        '[tool.pytest.ini_options]\npythonpath = ["."]\n',
        encoding="utf-8",
    )
    (worktree / "application.py").write_text(
        "def add(left: int, right: int) -> int:\n    return left + right\n",
        encoding="utf-8",
    )
    (worktree / "tests").mkdir()
    (worktree / "tests" / "test_application.py").write_text(
        "from application import add\n\n"
        "def test_addition():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    evidence = validation.validate(PID, MID, "first-application", now=NOW)
    assert evidence.decision is ValidationDecision.ACCEPT
    assert TRUSTED_CI_WORKFLOW_PATH not in {item.path for item in evidence.files}
    commit = workspaces.commit(
        PID,
        MID,
        evidence.trusted_head_sha,
        [item.path for item in evidence.files],
        "Implement the first tested Python application",
        NOW,
        expected_diff_hash=evidence.diff_hash,
    )
    assert commit.parent_sha == result.baseline_commit_sha
    assert (
        subprocess.check_output(
            ["git", "show", f"{commit.commit_sha}:{TRUSTED_CI_WORKFLOW_PATH}"],
            cwd=worktree,
        )
        == intent.workflow_content.encode()
    )
    test_step = intent.workflow_content.split(
        "      - name: Run real repository tests\n", 1
    )[1]
    command = textwrap.dedent(test_step.split("        run: |\n", 1)[1])
    execution = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command],
        cwd=worktree,
        env=dict(
            os.environ,
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert execution.returncode == 0, execution.stdout + execution.stderr
    assert "1 passed" in execution.stdout


@pytest.mark.parametrize("ambiguous_push", [False, True])
def test_restart_verifies_original_git_baseline_without_recreating_or_pushing(
    tmp_path: Path, ambiguous_push: bool
) -> None:
    connection = open_database(tmp_path / "state.db")
    apply_migrations(connection)
    _seed_approval(connection)
    service, first_remote, first_git = _service(connection, tmp_path)
    first_git.ambiguous_push = ambiguous_push
    first_remote.hide_after_configuration = True
    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "lost-verification")
    assert raised.value.failure is ProvisioningFailure.IDENTITY_MISMATCH
    _assert_not_ready(connection)
    original = SQLiteProvisioningRepository(connection).baseline(PID)
    original_ci = SQLiteProvisioningRepository(connection).ci_intent(PID)
    assert original is not None and original_ci is not None
    connection.close()

    # Every application/service/adapter/database object is reconstructed.
    connection = open_database(tmp_path / "state.db")
    try:
        recovered, remote, git = _service(connection, tmp_path)
        result = recovered.provision(PID, NOW, "restart")
        assert result.baseline_commit_sha == original.commit_sha
        assert result.project_state is ProjectState.READY
        assert (remote.create_calls, git.commit_calls, git.push_calls) == (0, 0, 0)
        assert SQLiteProvisioningRepository(connection).ci_intent(PID) == original_ci
        baseline = SQLiteProvisioningRepository(connection).baseline(PID)
        assert baseline is not None and baseline.id == original.id
        assert baseline.verified_at is not None
        assert _git(remote.remote_path, "rev-list", "--count", "main") == "1"
        assert (
            connection.execute(
                "SELECT count(*) FROM state_transitions WHERE new_state='READY'"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


@pytest.mark.parametrize("loss_point", ["after-local-commit", "before-remote-push"])
def test_restart_after_local_commit_reuses_exact_tree_and_durable_ci_intent(
    tmp_path: Path, loss_point: str
) -> None:
    connection = open_database(tmp_path / "state.db")
    apply_migrations(connection)
    _seed_approval(connection)
    service, remote, git = _service(connection, tmp_path)
    git.lose_after_commit = loss_point == "after-local-commit"
    git.lose_before_push = loss_point == "before-remote-push"
    with pytest.raises(SimulatedProcessLoss):
        service.provision(PID, NOW, "interrupted-local-baseline")
    original_sha = _git(git.root / str(PID), "rev-parse", "HEAD")
    original_ci = SQLiteProvisioningRepository(connection).ci_intent(PID)
    original_baseline = SQLiteProvisioningRepository(connection).baseline(PID)
    assert original_ci is not None
    assert (original_baseline is None) == (loss_point == "after-local-commit")
    assert remote.main_sha("fixture", "generated-python") is None
    assert (
        SQLiteProjectRepository(connection, lambda: "unused").get(PID).state
        is ProjectState.PROVISIONING
    )
    connection.close()

    connection = open_database(tmp_path / "state.db")
    try:
        recovered, remote, git = _service(connection, tmp_path)
        result = recovered.provision(PID, NOW, "replay-local-baseline")
        assert result.baseline_commit_sha == original_sha
        assert result.project_state is ProjectState.READY
        assert (remote.create_calls, git.commit_calls, git.push_calls) == (0, 1, 1)
        assert SQLiteProvisioningRepository(connection).ci_intent(PID) == original_ci
        assert _git(git.root / str(PID), "rev-list", "--count", "main") == "1"
        assert _git(remote.remote_path, "rev-list", "--count", "main") == "1"
        if original_baseline is not None:
            verified = SQLiteProvisioningRepository(connection).baseline(PID)
            assert verified is not None and verified.id == original_baseline.id
    finally:
        connection.close()


@pytest.mark.parametrize("content", [None, b"name: fake-ci\njobs: {}\n"])
def test_missing_or_changed_workflow_at_exact_baseline_sha_blocks_ready(
    database: sqlite3.Connection, tmp_path: Path, content: bytes | None
) -> None:
    service, remote, git = _service(database, tmp_path)
    remote.file_overrides[TRUSTED_CI_WORKFLOW_PATH] = content
    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "invalid-remote-workflow")
    assert raised.value.failure is ProvisioningFailure.CI_BASELINE_MISMATCH
    _assert_not_ready(database)
    baseline = SQLiteProvisioningRepository(database).baseline(PID)
    assert baseline is not None
    assert (baseline.commit_sha, TRUSTED_CI_WORKFLOW_PATH) in remote.reads
    assert git.push_calls == 1


def test_remote_main_sha_mismatch_never_verifies_workflow_or_marks_ready(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    service, remote, git = _service(database, tmp_path)
    remote.sha_override = "f" * 40
    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "invalid-remote-head")
    assert raised.value.failure is ProvisioningFailure.REMOTE_MISMATCH
    _assert_not_ready(database)
    assert remote.reads == [] and remote.configure_calls == 0
    assert git.push_calls == 1


@pytest.mark.parametrize("change", ["disable", "delete"])
def test_implementation_worker_cannot_replace_or_delete_generated_workflow(
    database: sqlite3.Connection, tmp_path: Path, change: str
) -> None:
    service, remote, _git_adapter = _service(database, tmp_path)
    result = service.provision(PID, NOW, "bootstrap")
    worktree, workspaces, validation = _workspace(database, tmp_path, remote)
    workflow = worktree / TRUSTED_CI_WORKFLOW_PATH
    original = workflow.read_bytes()
    if change == "disable":
        workflow.write_text("name: forged-success\njobs: {}\n", encoding="utf-8")
    else:
        workflow.unlink()
    (worktree / "application.py").write_text("def add(a, b): return a + b\n")

    rejected = validation.validate(PID, MID, "workflow-tampering", now=NOW)
    assert rejected.decision is ValidationDecision.REWORK_REQUIRED
    assert any(
        finding.code is FindingCode.PROTECTED_PATH_CHANGE
        and finding.path == TRUSTED_CI_WORKFLOW_PATH
        and finding.blocking
        for finding in rejected.findings
    )
    assert SecurityPolicy(database).blocked(PID, MID)
    with pytest.raises(WorkspaceError, match="accepted validation evidence"):
        workspaces.commit(
            PID,
            MID,
            rejected.trusted_head_sha,
            [item.path for item in rejected.files],
            "Attempt to disable required CI",
            NOW,
            expected_diff_hash=rejected.diff_hash,
        )
    assert _git(worktree, "rev-parse", "HEAD") == result.baseline_commit_sha
    assert database.execute("SELECT count(*) FROM commits").fetchone()[0] == 0
    assert (
        remote.file_content(
            "fixture",
            "generated-python",
            result.baseline_commit_sha,
            TRUSTED_CI_WORKFLOW_PATH,
        )
        == original
    )
