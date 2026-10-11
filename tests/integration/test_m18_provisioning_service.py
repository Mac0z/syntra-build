from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.application.ci_profiles import select_ci_profile
from syntra_build.application.provisioning import (
    AmbiguousGitHubResult,
    AmbiguousPushResult,
    ProvisioningError,
    ProvisioningFailure,
    RemoteRepository,
    RepositoryProvisioningService,
)
from syntra_build.application.security import SecurityPolicy
from syntra_build.domain import (
    ProjectDocumentId,
    ProjectId,
    ProjectState,
    RepositoryVisibility,
)
from syntra_build.domain.provisioning import RepositoryCIIntent
from syntra_build.domain.security import (
    SecurityActorType,
    SecurityEventType,
    SecurityResolutionCode,
    SecuritySeverity,
)
from syntra_build.infrastructure.persistence import (
    SQLiteProjectRepository,
    SQLiteProvisioningRepository,
    apply_migrations,
    open_database,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PID = ProjectId(UUID(int=1))
SPEC_ID = "00000000-0000-0000-0000-000000000002"
AGENTS_ID = "00000000-0000-0000-0000-000000000003"
PACKAGE_ID = "00000000-0000-0000-0000-000000000004"
GATE_ID = "00000000-0000-0000-0000-000000000005"
SHA = "a" * 40
SPEC = (
    "# Approved specification\n\n"
    '```syntra-ci\nprofile = "python-pytest-v1"\npython-version = "3.12"\n```\n'
)
AGENTS = "# Approved agent rules\n"
BASELINE_FILES = {
    "SPEC.md": SPEC.encode(),
    "AGENTS.md": AGENTS.encode(),
    **select_ci_profile(SPEC).workflow_files(),
}


def _hash(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode()).hexdigest()


def _approved_project(
    connection: sqlite3.Connection,
    visibility: RepositoryVisibility = RepositoryVisibility.PUBLIC,
    *,
    spec_content: str = SPEC,
    agents_content: str = AGENTS,
) -> None:
    now = NOW.isoformat()
    connection.execute(
        """INSERT INTO projects
        (id,name,state,created_at,updated_at,last_state_change_at,canonical_name,
         repository_visibility) VALUES (?,?,?,?,?,?,?,?)""",
        (
            str(PID),
            "My Cool App",
            "PROVISIONING",
            now,
            now,
            now,
            "my-cool-app",
            visibility.value,
        ),
    )
    for document_id, kind, content in (
        (SPEC_ID, "SPEC", spec_content),
        (AGENTS_ID, "AGENTS", agents_content),
    ):
        connection.execute(
            """INSERT INTO project_documents
            (id,project_id,document_type,revision,status,content,content_hash,
             created_at,created_by,approved_at,approved_by)
            VALUES (?,?,?,1,'DRAFT',?,?,?,?,NULL,NULL)""",
            (document_id, str(PID), kind, content, _hash(content), now, "architect"),
        )
    connection.execute(
        """INSERT INTO architect_requests
        (id,project_id,request_type,provider,model,reasoning_level,
         request_schema_version,request_payload_json,correlation_id,started_at,
         completed_at,status)
        VALUES ('request',?,'SPECIFICATION_DRAFT','fake','fake','high','1','{}',
                'correlation',?,?,'SUCCEEDED')""",
        (str(PID), now, now),
    )
    connection.execute(
        """INSERT INTO human_gates
        (id,project_id,gate_type,state,title,prompt,expected_response_type,
         options_json,created_at,created_by,correlation_id)
        VALUES (?,?,'DESIGN_APPROVAL','RESOLVED','Approve','Approve?',
                'DESIGN_APPROVAL','[]',?,'system','correlation')""",
        (GATE_ID, str(PID), now),
    )
    connection.execute(
        """INSERT INTO design_packages
        (id,project_id,architect_request_id,spec_document_id,agents_document_id,
         repository_visibility,design_summary,planned_milestones_json,
         assumptions_json,non_blocking_issues_json,status,approval_gate_id,
         created_at)
        VALUES (?,?,?,?,?,?,'summary','[]','[]','[]','PENDING_APPROVAL',?,?)""",
        (
            PACKAGE_ID,
            str(PID),
            "request",
            SPEC_ID,
            AGENTS_ID,
            visibility.value,
            GATE_ID,
            now,
        ),
    )
    connection.execute(
        """UPDATE project_documents
        SET status='APPROVED',approved_at=?,approved_by='human'
        WHERE project_id=?""",
        (now, str(PID)),
    )
    connection.execute(
        """UPDATE design_packages
        SET status='APPROVED',approved_at=?,approved_by='human'
        WHERE id=?""",
        (now, PACKAGE_ID),
    )
    connection.commit()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_database(tmp_path / "state.db")
    apply_migrations(connection)
    _approved_project(connection)
    yield connection
    connection.close()


class FakeGitHub:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.remote: RemoteRepository | None = None
        self.create_calls = 0
        self.configure_calls = 0
        self.lookup_calls = 0
        self.main: str | None = None
        self.contents: dict[str, bytes] = {}
        self.create_effects: list[str] = []
        self.disappear_on_final_lookup = False
        self.configure_updates_default = True
        self._configured = False

    def get_repository(self, owner: str, name: str) -> RemoteRepository | None:
        self.lookup_calls += 1
        if self.disappear_on_final_lookup and self._configured:
            return None
        return self.remote

    def create_repository(
        self, name: str, visibility: RepositoryVisibility
    ) -> RemoteRepository:
        self.create_calls += 1
        row = self.connection.execute(
            "SELECT repository_name,visibility,status FROM github_repositories"
        ).fetchone()
        assert tuple(row) in {
            (name, visibility.value, "INTENDED"),
            (name, visibility.value, "CREATE_AMBIGUOUS"),
        }
        self.remote = RemoteRepository(
            123, "Mac0z", name, f"Mac0z/{name}", visibility, None
        )
        effect = self.create_effects.pop(0) if self.create_effects else "success"
        if effect == "ambiguous-created":
            raise AmbiguousGitHubResult("timed out")
        if effect == "ambiguous-absent":
            self.remote = None
            raise AmbiguousGitHubResult("timed out")
        return self.remote

    def configure_repository(self, owner: str, name: str) -> None:
        self.configure_calls += 1
        self._configured = True
        assert self.remote is not None
        if self.configure_updates_default:
            self.remote = RemoteRepository(
                self.remote.external_id,
                owner,
                name,
                f"{owner}/{name}",
                self.remote.visibility,
                "main",
            )

    def main_sha(self, owner: str, name: str) -> str | None:
        return self.main

    def file_content(self, owner: str, name: str, sha: str, path: str) -> bytes | None:
        state = (
            SQLiteProjectRepository(self.connection, lambda: "unused").get(PID).state
        )
        assert state is ProjectState.PROVISIONING
        return self.contents.get(path)


class FakeGit:
    def __init__(self, github: FakeGitHub) -> None:
        self.github = github
        self.commit_calls = 0
        self.push_calls = 0
        self.files: dict[str, bytes] = {}
        self.remote_url: str | None = None
        self.ambiguous_push = False
        self.publish_on_ambiguous = True

    def create_commit(
        self, project_id: ProjectId, files: dict[str, bytes], message: str
    ) -> str:
        self.commit_calls += 1
        self.files = files
        assert message == "Initial approved project design"
        return SHA

    def push_main(
        self, project_id: ProjectId, remote_url: str, expected_sha: str
    ) -> None:
        self.push_calls += 1
        self.remote_url = remote_url
        if not self.ambiguous_push or self.publish_on_ambiguous:
            self.github.main = expected_sha
            self.github.contents = dict(self.files)
        if self.ambiguous_push:
            raise AmbiguousPushResult("connection lost")


def _service(
    connection: sqlite3.Connection, github: FakeGitHub | None = None
) -> tuple[RepositoryProvisioningService, FakeGitHub, FakeGit]:
    github = github or FakeGitHub(connection)
    git = FakeGit(github)
    return (
        RepositoryProvisioningService(connection, github, git, owner="Mac0z"),
        github,
        git,
    )


@pytest.mark.parametrize(
    "visibility", [RepositoryVisibility.PUBLIC, RepositoryVisibility.PRIVATE]
)
def test_successfully_provisions_and_independently_verifies_baseline(
    tmp_path: Path, visibility: RepositoryVisibility
) -> None:
    connection = open_database(tmp_path / "state.db")
    apply_migrations(connection)
    _approved_project(connection, visibility)
    service, github, git = _service(connection)

    result = service.provision(PID, NOW, "correlation")

    project = SQLiteProjectRepository(connection, lambda: "unused").get(PID)
    repository = SQLiteProvisioningRepository(connection).for_project(PID)
    baseline = SQLiteProvisioningRepository(connection).baseline(PID)
    assert result.project_name == project.name == "My Cool App"
    assert project.canonical_name == "my-cool-app"
    assert project.state is ProjectState.READY
    assert github.create_calls == git.push_calls == 1
    assert git.remote_url == "https://github.com/Mac0z/my-cool-app.git"
    assert git.files == dict(BASELINE_FILES)
    assert repository is not None
    assert repository.external_repository_id == 123
    assert repository.visibility is visibility
    assert baseline is not None
    assert (
        baseline.spec_document_id.value,
        baseline.spec_revision,
        baseline.spec_content_hash,
    ) == (UUID(SPEC_ID), 1, _hash(SPEC))
    assert (
        baseline.agents_document_id.value,
        baseline.agents_revision,
        baseline.agents_content_hash,
    ) == (UUID(AGENTS_ID), 1, _hash(AGENTS))
    assert baseline.commit_sha == github.main == SHA
    assert baseline.verified_at is not None
    transition = connection.execute(
        """SELECT previous_state,new_state,reason FROM state_transitions
        WHERE project_id=? ORDER BY created_at DESC LIMIT 1""",
        (str(PID),),
    ).fetchone()
    assert tuple(transition) == (
        "PROVISIONING",
        "READY",
        "GitHub baseline independently verified",
    )
    connection.close()


@pytest.mark.parametrize(
    ("mutation", "failure"),
    [
        (
            lambda db: db.execute("UPDATE projects SET state='READY'"),
            ProvisioningFailure.PRECONDITION,
        ),
        (
            lambda db: db.execute(
                """UPDATE design_packages
                SET status='REJECTED',rejected_at=?,approved_at=NULL""",
                (NOW.isoformat(),),
            ),
            ProvisioningFailure.PRECONDITION,
        ),
        (
            lambda db: db.execute(
                "UPDATE projects SET repository_visibility='private'"
            ),
            ProvisioningFailure.PRECONDITION,
        ),
        (
            lambda db: db.execute(
                """UPDATE project_documents
                SET status='DRAFT',approved_at=NULL,approved_by=NULL
                WHERE document_type='SPEC'"""
            ),
            ProvisioningFailure.DOCUMENT_MISMATCH,
        ),
        (
            lambda db: db.execute(
                """UPDATE project_documents
                SET status='DRAFT',approved_at=NULL,approved_by=NULL
                WHERE document_type='AGENTS'"""
            ),
            ProvisioningFailure.DOCUMENT_MISMATCH,
        ),
    ],
)
def test_preconditions_fail_before_external_mutation(
    database: sqlite3.Connection,
    mutation: Callable[[sqlite3.Connection], object],
    failure: ProvisioningFailure,
) -> None:
    mutation(database)
    database.commit()
    service, github, git = _service(database)
    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "correlation")
    assert raised.value.failure is failure
    assert github.create_calls == git.commit_calls == git.push_calls == 0


@pytest.mark.parametrize("kind", ["SPEC", "AGENTS"])
def test_persisted_document_hash_mismatch_blocks_before_mutation(
    database: sqlite3.Connection, kind: str
) -> None:
    database.execute("DROP TRIGGER project_documents_content_immutable")
    database.execute(
        """UPDATE project_documents SET content=content || 'tampered'
        WHERE document_type=?""",
        (kind,),
    )
    database.commit()
    service, github, git = _service(database)
    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "correlation")
    assert raised.value.failure is ProvisioningFailure.DOCUMENT_MISMATCH
    assert github.create_calls == git.push_calls == 0


def test_unattributed_existing_name_is_a_collision(
    database: sqlite3.Connection,
) -> None:
    service, github, git = _service(database)
    github.remote = RemoteRepository(
        999,
        "Mac0z",
        "my-cool-app",
        "Mac0z/my-cool-app",
        RepositoryVisibility.PUBLIC,
        "main",
    )
    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "correlation")
    assert raised.value.failure is ProvisioningFailure.COLLISION
    assert github.create_calls == git.push_calls == 0
    assert (
        SQLiteProjectRepository(database, lambda: "unused").get(PID).state
        is ProjectState.PROVISIONING
    )


@pytest.mark.parametrize(
    ("effects", "expected_calls"),
    [(["ambiguous-created"], 1), (["ambiguous-absent", "success"], 2)],
)
def test_ambiguous_create_reconciles_before_bounded_retry(
    database: sqlite3.Connection, effects: list[str], expected_calls: int
) -> None:
    service, github, _ = _service(database)
    github.create_effects = effects
    assert service.provision(PID, NOW, "correlation").verified
    assert github.create_calls == expected_calls
    repository = SQLiteProvisioningRepository(database).for_project(PID)
    assert repository is not None
    assert repository.external_repository_id == 123


def test_restart_reconciles_persisted_ambiguous_create(
    database: sqlite3.Connection,
) -> None:
    records = SQLiteProvisioningRepository(database)
    intent = records.ensure_intent(
        PID, "Mac0z", "my-cool-app", RepositoryVisibility.PUBLIC, NOW
    )
    _save_ci_intent(records, intent.id)
    records.mark_ambiguous(intent.id, NOW)
    database.commit()
    service, github, _ = _service(database)
    github.remote = RemoteRepository(
        123,
        "Mac0z",
        "my-cool-app",
        "Mac0z/my-cool-app",
        RepositoryVisibility.PUBLIC,
        None,
    )
    service.provision(PID, NOW, "correlation")
    assert github.create_calls == 0


@pytest.mark.parametrize(
    "remote",
    [
        RemoteRepository(
            456,
            "Mac0z",
            "my-cool-app",
            "Mac0z/my-cool-app",
            RepositoryVisibility.PUBLIC,
            "main",
        ),
        RemoteRepository(
            123,
            "Other",
            "my-cool-app",
            "Other/my-cool-app",
            RepositoryVisibility.PUBLIC,
            "main",
        ),
        RemoteRepository(
            123, "Mac0z", "other", "Mac0z/other", RepositoryVisibility.PUBLIC, "main"
        ),
        RemoteRepository(
            123,
            "Mac0z",
            "my-cool-app",
            "Mac0z/wrong-full-name",
            RepositoryVisibility.PUBLIC,
            "main",
        ),
        RemoteRepository(
            123,
            "Mac0z",
            "my-cool-app",
            "Mac0z/my-cool-app",
            RepositoryVisibility.PRIVATE,
            "main",
        ),
    ],
)
def test_persisted_identity_mismatch_fails_closed(
    database: sqlite3.Connection, remote: RemoteRepository
) -> None:
    records = SQLiteProvisioningRepository(database)
    intent = records.ensure_intent(
        PID, "Mac0z", "my-cool-app", RepositoryVisibility.PUBLIC, NOW
    )
    _save_ci_intent(records, intent.id)
    records.identify(intent.id, 123, "main", NOW)
    database.commit()
    service, github, git = _service(database)
    github.remote = remote
    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "correlation")
    assert raised.value.failure in {
        ProvisioningFailure.IDENTITY_MISMATCH,
        ProvisioningFailure.VISIBILITY_MISMATCH,
    }
    assert git.push_calls == 0


def _save_ci_intent(records: SQLiteProvisioningRepository, repository_id: str) -> None:
    profile = select_ci_profile(SPEC)
    workflow = profile.workflow_content()
    records.save_ci_intent(
        RepositoryCIIntent(
            project_id=PID,
            github_repository_id=repository_id,
            spec_document_id=ProjectDocumentId.from_string(SPEC_ID),
            spec_revision=1,
            spec_content_hash=_hash(SPEC),
            agents_document_id=ProjectDocumentId.from_string(AGENTS_ID),
            agents_revision=1,
            agents_content_hash=_hash(AGENTS),
            profile_id=profile.profile_id,
            python_version=profile.python_version,
            workflow_path=".github/workflows/ci.yml",
            workflow_content=workflow.decode(),
            workflow_content_hash=_hash(workflow.decode()),
            default_branch="main",
            created_at=NOW,
        )
    )


def _identified_with_baseline(
    database: sqlite3.Connection,
) -> tuple[RepositoryProvisioningService, FakeGitHub, FakeGit]:
    service, github, git = _service(database)
    github.remote = RemoteRepository(
        123,
        "Mac0z",
        "my-cool-app",
        "Mac0z/my-cool-app",
        RepositoryVisibility.PUBLIC,
        "main",
    )
    records = SQLiteProvisioningRepository(database)
    intent = records.ensure_intent(
        PID, "Mac0z", "my-cool-app", RepositoryVisibility.PUBLIC, NOW
    )
    _save_ci_intent(records, intent.id)
    records.identify(intent.id, 123, "main", NOW)
    database.commit()
    return service, github, git


def test_existing_expected_remote_main_is_not_rewritten(
    database: sqlite3.Connection,
) -> None:
    service, github, git = _identified_with_baseline(database)
    github.main = SHA
    github.contents = dict(BASELINE_FILES)
    service.provision(PID, NOW, "correlation")
    assert git.push_calls == 0


def test_retry_reuses_attributed_empty_repository_and_pushes_baseline(
    database: sqlite3.Connection,
) -> None:
    service, github, git = _identified_with_baseline(database)
    assert github.main is None

    result = service.provision(PID, NOW, "empty-repository-recovery")

    repository = SQLiteProvisioningRepository(database).for_project(PID)
    baseline = SQLiteProvisioningRepository(database).baseline(PID)
    assert result.project_state is ProjectState.READY
    assert github.create_calls == 0
    assert git.commit_calls == 1
    assert git.push_calls == 1
    assert git.files == dict(BASELINE_FILES)
    assert github.main == SHA
    assert github.contents == git.files
    assert repository is not None
    assert repository.external_repository_id == 123
    assert repository.visibility is RepositoryVisibility.PUBLIC
    assert baseline is not None
    assert baseline.commit_sha == SHA
    assert baseline.spec_document_id.value == UUID(SPEC_ID)
    assert baseline.spec_content_hash == _hash(SPEC)
    assert baseline.agents_document_id.value == UUID(AGENTS_ID)
    assert baseline.agents_content_hash == _hash(AGENTS)
    transition_count = database.execute(
        """SELECT count(*) FROM state_transitions
        WHERE project_id=? AND previous_state='PROVISIONING' AND new_state='READY'""",
        (str(PID),),
    ).fetchone()[0]
    assert transition_count == 1


def test_ambiguous_push_reconciles_expected_remote_sha(
    database: sqlite3.Connection,
) -> None:
    service, github, git = _identified_with_baseline(database)
    git.ambiguous_push = True
    service.provision(PID, NOW, "correlation")
    assert git.push_calls == 1


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unexpected_or_unconfirmed_remote_sha_blocks_ready(
    database: sqlite3.Connection, ambiguous: bool
) -> None:
    service, github, git = _identified_with_baseline(database)
    if ambiguous:
        git.ambiguous_push = True
        git.publish_on_ambiguous = False
    else:
        github.main = "b" * 40
    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "correlation")
    assert raised.value.failure is ProvisioningFailure.REMOTE_MISMATCH
    assert (
        SQLiteProjectRepository(database, lambda: "unused").get(PID).state
        is ProjectState.PROVISIONING
    )
    assert git.push_calls == (1 if ambiguous else 0)


@pytest.mark.parametrize(
    ("contents", "default_branch", "disappears", "failure"),
    [
        (
            {"SPEC.md": b"wrong", "AGENTS.md": AGENTS.encode()},
            "main",
            False,
            ProvisioningFailure.DOCUMENT_MISMATCH,
        ),
        (
            {"SPEC.md": SPEC.encode(), "AGENTS.md": b"wrong"},
            "main",
            False,
            ProvisioningFailure.DOCUMENT_MISMATCH,
        ),
        (
            {"AGENTS.md": AGENTS.encode()},
            "main",
            False,
            ProvisioningFailure.DOCUMENT_MISMATCH,
        ),
        (
            {"SPEC.md": SPEC.encode()},
            "main",
            False,
            ProvisioningFailure.DOCUMENT_MISMATCH,
        ),
        (
            dict(BASELINE_FILES),
            "master",
            False,
            ProvisioningFailure.REMOTE_MISMATCH,
        ),
        (
            dict(BASELINE_FILES),
            "main",
            True,
            ProvisioningFailure.IDENTITY_MISMATCH,
        ),
    ],
)
def test_remote_baseline_verification_failures_prevent_ready(
    database: sqlite3.Connection,
    contents: dict[str, bytes],
    default_branch: str,
    disappears: bool,
    failure: ProvisioningFailure,
) -> None:
    service, github, git = _identified_with_baseline(database)
    github.main = SHA
    github.contents = contents
    github.remote = RemoteRepository(
        123,
        "Mac0z",
        "my-cool-app",
        "Mac0z/my-cool-app",
        RepositoryVisibility.PUBLIC,
        default_branch,
    )
    github.disappear_on_final_lookup = disappears
    github.configure_updates_default = default_branch == "main"
    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "correlation")
    assert raised.value.failure is failure
    assert (
        SQLiteProjectRepository(database, lambda: "unused").get(PID).state
        is ProjectState.PROVISIONING
    )
    assert git.push_calls == 0


def test_inconsistent_existing_baseline_is_not_replaced(
    database: sqlite3.Connection,
) -> None:
    service, github, git = _identified_with_baseline(database)
    repository = SQLiteProvisioningRepository(database).for_project(PID)
    assert repository is not None
    database.execute(
        """INSERT INTO repository_baselines VALUES
        ('baseline',?,?,?,?,1,?,?,1,?,?,NULL)""",
        (
            str(PID),
            repository.id,
            SHA,
            AGENTS_ID,
            _hash(AGENTS),
            SPEC_ID,
            _hash(SPEC),
            NOW.isoformat(),
        ),
    )
    database.commit()
    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "correlation")
    assert raised.value.failure is ProvisioningFailure.DOCUMENT_MISMATCH
    assert git.commit_calls == git.push_calls == 0


def test_restart_reuses_identified_repository_and_durable_baseline(
    database: sqlite3.Connection,
) -> None:
    service, github, first_git = _service(database)
    github.disappear_on_final_lookup = True
    with pytest.raises(ProvisioningError):
        service.provision(PID, NOW, "first-attempt")
    assert first_git.commit_calls == first_git.push_calls == github.create_calls == 1
    persisted = SQLiteProvisioningRepository(database).baseline(PID)
    assert persisted is not None

    second_git = FakeGit(github)
    second_service = RepositoryProvisioningService(
        database, github, second_git, owner="Mac0z"
    )
    github.disappear_on_final_lookup = False
    result = second_service.provision(PID, NOW, "recovery-attempt")

    assert result.project_state is ProjectState.READY
    assert github.create_calls == 1
    assert second_git.commit_calls == second_git.push_calls == 0
    verified = SQLiteProvisioningRepository(database).baseline(PID)
    assert verified is not None
    assert verified.id == persisted.id
    assert verified.commit_sha == persisted.commit_sha
    assert verified.spec_document_id == persisted.spec_document_id
    assert verified.agents_document_id == persisted.agents_document_id
    transition_count = database.execute(
        """SELECT count(*) FROM state_transitions
        WHERE project_id=? AND previous_state='PROVISIONING' AND new_state='READY'""",
        (str(PID),),
    ).fetchone()[0]
    assert transition_count == 1


@pytest.mark.parametrize("document_type", ["SPEC", "AGENTS"])
def test_approved_design_secret_blocks_before_repository_mutation(
    tmp_path: Path, document_type: str
) -> None:
    synthetic = "api_key = 'sk-proj-SYNTHETIC0123456789abcdefghijk'\n"
    database = open_database(tmp_path / "secret-state.db")
    apply_migrations(database)
    _approved_project(
        database,
        spec_content=synthetic if document_type == "SPEC" else SPEC,
        agents_content=synthetic if document_type == "AGENTS" else AGENTS,
    )
    service, github, git = _service(database)

    with pytest.raises(ProvisioningError) as raised:
        service.provision(PID, NOW, "design-secret")

    assert raised.value.failure is ProvisioningFailure.SECURITY_BLOCKED
    assert github.create_calls == git.commit_calls == git.push_calls == 0
    event = database.execute(
        "SELECT safe_details_json FROM security_events WHERE correlation_id=?",
        ("design-secret",),
    ).fetchone()
    assert event is not None
    assert synthetic.strip() not in event["safe_details_json"]


def test_persisted_security_condition_blocks_all_provisioning_mutations(
    database: sqlite3.Connection,
) -> None:
    service, github, git = _service(database)
    policy = SecurityPolicy(database)
    event = policy.record(
        SecurityEventType.PROTECTED_PATH_CHANGE,
        SecuritySeverity.HIGH,
        project_id=PID,
        source_component="test_policy",
        correlation_id="block-provisioning",
        blocking=True,
        created_at=NOW,
    )

    with pytest.raises(ProvisioningError, match="security policy"):
        service.provision(PID, NOW, "blocked")
    assert (github.create_calls, github.configure_calls, git.push_calls) == (0, 0, 0)

    policy.resolve(
        event.id,
        SecurityResolutionCode.CLEAN_REVALIDATION,
        "trusted-resolution",
        SecurityActorType.SYSTEM,
        resolved_at=NOW,
    )
    assert service.provision(PID, NOW, "allowed").verified


def test_security_condition_appearing_after_discovery_blocks_create(
    database: sqlite3.Connection,
) -> None:
    class BlockingLookup(FakeGitHub):
        def get_repository(self, owner: str, name: str) -> RemoteRepository | None:
            result = super().get_repository(owner, name)
            if self.lookup_calls == 1:
                SecurityPolicy(self.connection).record(
                    SecurityEventType.REPOSITORY_IDENTITY_MISMATCH,
                    SecuritySeverity.CRITICAL,
                    project_id=PID,
                    source_component="test_race",
                    correlation_id="race",
                    blocking=True,
                    created_at=NOW,
                )
            return result

    blocking_github = BlockingLookup(database)
    service, observed_github, git = _service(database, blocking_github)
    with pytest.raises(ProvisioningError, match="security policy"):
        service.provision(PID, NOW, "race")
    assert (
        observed_github.create_calls,
        observed_github.configure_calls,
        git.push_calls,
    ) == (0, 0, 0)


@pytest.mark.parametrize(
    "spec_content",
    [
        "# Python project without an approved CI profile\n",
        SPEC.replace("python-pytest-v1", "node-test-v1"),
        SPEC.replace("3.12", "3.11"),
        SPEC + "\n" + SPEC,
    ],
)
def test_missing_or_unsupported_profile_has_no_repository_side_effects(
    tmp_path: Path, spec_content: str
) -> None:
    with open_database(tmp_path / "unsupported.db") as database:
        apply_migrations(database)
        _approved_project(database, spec_content=spec_content)
        service, github, git = _service(database)
        with pytest.raises(ProvisioningError) as error:
            service.provision(PID, NOW, "unsupported-profile")
        assert error.value.failure is ProvisioningFailure.CI_PROFILE_REQUIRED
        assert "approved CI profile" in str(error.value)
        assert github.lookup_calls == github.create_calls == github.configure_calls == 0
        assert git.commit_calls == git.push_calls == 0
        assert SQLiteProvisioningRepository(database).for_project(PID) is None


def test_legacy_baseline_requires_explicit_recovery_without_rewrite(
    database: sqlite3.Connection,
) -> None:
    records = SQLiteProvisioningRepository(database)
    repository = records.ensure_intent(
        PID, "Mac0z", "my-cool-app", RepositoryVisibility.PUBLIC, NOW
    )
    records.identify(repository.id, 123, "main", NOW)
    database.execute(
        """INSERT INTO repository_baselines VALUES
        ('legacy',?,?,?,?,1,?,?,1,?,?,NULL)""",
        (
            str(PID),
            repository.id,
            SHA,
            SPEC_ID,
            _hash(SPEC),
            AGENTS_ID,
            _hash(AGENTS),
            NOW.isoformat(),
        ),
    )
    database.commit()
    original_baseline = records.baseline(PID)
    original_repository = records.for_project(PID)
    service, github, git = _service(database)
    with pytest.raises(
        ProvisioningError, match="separately approved recovery"
    ) as error:
        service.provision(PID, NOW, "legacy")
    assert error.value.failure is ProvisioningFailure.CI_BASELINE_MISMATCH
    assert records.baseline(PID) == original_baseline
    assert records.for_project(PID) == original_repository
    assert records.ci_intent(PID) is None
    assert github.lookup_calls == github.configure_calls == github.create_calls == 0
    assert git.commit_calls == git.push_calls == 0


@pytest.mark.parametrize("mutation", ["workflow", "hash", "path", "branch", "profile"])
def test_persisted_ci_corruption_blocks_before_provider_calls(
    database: sqlite3.Connection, mutation: str
) -> None:
    _, _, _ = _identified_with_baseline(database)
    records = SQLiteProvisioningRepository(database)
    intent = records.ci_intent(PID)
    assert intent is not None
    # Model damaged persistent evidence, beyond the ordinary immutable SQL boundary.
    database.execute("DROP TRIGGER repository_ci_intents_no_update")
    database.execute("PRAGMA ignore_check_constraints=ON")
    if mutation == "workflow":
        content = intent.workflow_content + "# substituted template\n"
        database.execute(
            "UPDATE repository_ci_intents "
            "SET workflow_content=?,workflow_content_hash=?",
            (content, _hash(content)),
        )
    elif mutation == "hash":
        database.execute(
            "UPDATE repository_ci_intents SET workflow_content_hash=?", ("0" * 64,)
        )
    elif mutation == "path":
        database.execute(
            "UPDATE repository_ci_intents "
            "SET workflow_path='.github/workflows/other.yml'"
        )
    elif mutation == "branch":
        database.execute("UPDATE repository_ci_intents SET default_branch='develop'")
    else:
        database.execute("UPDATE repository_ci_intents SET profile_id='node-test-v1'")
    database.commit()
    service, github, git = _service(database)
    with pytest.raises(ProvisioningError) as error:
        service.provision(PID, NOW, "damaged-evidence")
    assert error.value.failure is ProvisioningFailure.CI_BASELINE_MISMATCH
    assert github.lookup_calls == github.create_calls == github.configure_calls == 0
    assert git.commit_calls == git.push_calls == 0


def test_persisted_baseline_replay_checks_local_sha_before_push(
    database: sqlite3.Connection,
) -> None:
    service, github, git = _service(database)
    git.ambiguous_push = True
    git.publish_on_ambiguous = False
    with pytest.raises(ProvisioningError):
        service.provision(PID, NOW, "persist-first")
    persisted = SQLiteProvisioningRepository(database).baseline(PID)
    assert persisted is not None
    original_pushes = git.push_calls

    class WrongLocalGit(FakeGit):
        def create_commit(
            self, project_id: ProjectId, files: dict[str, bytes], message: str
        ) -> str:
            super().create_commit(project_id, files, message)
            return "c" * 40

    wrong_git = WrongLocalGit(github)
    with pytest.raises(ProvisioningError, match="local replay") as error:
        RepositoryProvisioningService(
            database, github, wrong_git, owner="Mac0z"
        ).provision(PID, NOW, "reject-stale-local")
    assert error.value.failure is ProvisioningFailure.REMOTE_MISMATCH
    assert git.push_calls == original_pushes
    assert wrong_git.push_calls == 0
    assert SQLiteProvisioningRepository(database).baseline(PID) == persisted


def test_wrong_default_branch_is_rejected_before_push_or_configuration(
    database: sqlite3.Connection,
) -> None:
    service, github, git = _identified_with_baseline(database)
    assert github.remote is not None
    from dataclasses import replace

    github.remote = replace(github.remote, default_branch="develop")
    with pytest.raises(ProvisioningError) as error:
        service.provision(PID, NOW, "wrong-default")
    assert error.value.failure is ProvisioningFailure.REMOTE_MISMATCH
    assert git.commit_calls == git.push_calls == github.configure_calls == 0


def test_security_condition_during_workflow_verification_blocks_ready(
    database: sqlite3.Connection,
) -> None:
    class BlockingWorkflowRead(FakeGitHub):
        def file_content(
            self, owner: str, name: str, sha: str, path: str
        ) -> bytes | None:
            content = super().file_content(owner, name, sha, path)
            if path == ".github/workflows/ci.yml":
                SecurityPolicy(self.connection).record(
                    SecurityEventType.REPOSITORY_IDENTITY_MISMATCH,
                    SecuritySeverity.HIGH,
                    project_id=PID,
                    source_component="final-workflow-verification",
                    correlation_id="late-security-condition",
                    blocking=True,
                    created_at=NOW,
                )
            return content

    service, github, git = _service(database, BlockingWorkflowRead(database))
    with pytest.raises(ProvisioningError) as error:
        service.provision(PID, NOW, "late-security-condition")
    assert error.value.failure is ProvisioningFailure.SECURITY_BLOCKED
    assert github.create_calls == git.commit_calls == git.push_calls == 1
    records = SQLiteProvisioningRepository(database)
    baseline = records.baseline(PID)
    repository = records.for_project(PID)
    assert baseline is not None and baseline.verified_at is None
    assert repository is not None and repository.verified_at is None
    assert SQLiteProjectRepository(database, lambda: "unused").get(PID).state is (
        ProjectState.PROVISIONING
    )


def test_main_changed_during_workflow_verification_cannot_be_verified(
    database: sqlite3.Connection,
) -> None:
    class ChangingMain(FakeGitHub):
        def file_content(
            self, owner: str, name: str, sha: str, path: str
        ) -> bytes | None:
            content = super().file_content(owner, name, sha, path)
            if path == ".github/workflows/ci.yml":
                self.main = "d" * 40
            return content

    service, _, _ = _service(database, ChangingMain(database))
    with pytest.raises(ProvisioningError, match="changed during") as error:
        service.provision(PID, NOW, "changed-main")
    assert error.value.failure is ProvisioningFailure.REMOTE_MISMATCH
    records = SQLiteProvisioningRepository(database)
    baseline = records.baseline(PID)
    repository = records.for_project(PID)
    assert baseline is not None and baseline.verified_at is None
    assert repository is not None and repository.verified_at is None
    assert SQLiteProjectRepository(database, lambda: "unused").get(PID).state is (
        ProjectState.PROVISIONING
    )
