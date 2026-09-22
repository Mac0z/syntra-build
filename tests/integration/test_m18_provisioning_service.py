from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.application.provisioning import (
    AmbiguousGitHubResult,
    AmbiguousPushResult,
    ProvisioningError,
    ProvisioningFailure,
    RemoteRepository,
    RepositoryProvisioningService,
)
from syntra_build.domain import ProjectId, ProjectState, RepositoryVisibility
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
SPEC = "# Approved specification\n"
AGENTS = "# Approved agent rules\n"


def _hash(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode()).hexdigest()


def _approved_project(
    connection: sqlite3.Connection,
    visibility: RepositoryVisibility = RepositoryVisibility.PUBLIC,
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
        (SPEC_ID, "SPEC", SPEC),
        (AGENTS_ID, "AGENTS", AGENTS),
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
    assert git.files == {"SPEC.md": SPEC.encode(), "AGENTS.md": AGENTS.encode()}
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
    records.identify(intent.id, 123, "main", NOW)
    database.commit()
    return service, github, git


def test_existing_expected_remote_main_is_not_rewritten(
    database: sqlite3.Connection,
) -> None:
    service, github, git = _identified_with_baseline(database)
    github.main = SHA
    github.contents = {"SPEC.md": SPEC.encode(), "AGENTS.md": AGENTS.encode()}
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
    assert git.files == {"SPEC.md": SPEC.encode(), "AGENTS.md": AGENTS.encode()}
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
            {"SPEC.md": SPEC.encode(), "AGENTS.md": AGENTS.encode()},
            "master",
            False,
            ProvisioningFailure.REMOTE_MISMATCH,
        ),
        (
            {"SPEC.md": SPEC.encode(), "AGENTS.md": AGENTS.encode()},
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
