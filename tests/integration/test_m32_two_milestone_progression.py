# ruff: noqa: E501
"""M32.18 credential-free acceptance of two sequential milestones."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from syntra_build.application.architect import ArchitectProvider
from syntra_build.application.codex import BoundCodexRunner
from syntra_build.application.lifecycle import STATE_JOBS
from syntra_build.application.production import (
    PRODUCTION_JOB_TYPES,
    ProductionDependencies,
    ProductionDueWorkCoordinator,
    build_production_executors,
)
from syntra_build.application.scheduler import JobExecutor, Scheduler, WorkerCapacity
from syntra_build.application.workspaces import WorkspaceService, milestone_branch_name
from syntra_build.domain import (
    ARCHITECT_TASK_INTERFACE_VERSION,
    ArchitectTask,
    ArchitectTaskRequest,
    ArchitectTaskType,
    Milestone,
    MilestoneId,
    MilestoneState,
    Project,
    ProjectId,
    ProjectState,
    WorkerClass,
)
from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIObservation,
)
from syntra_build.domain.codex import (
    CodexProcessStatus,
    CodexRunRequest,
    CodexRunResult,
)
from syntra_build.domain.design import ARCHITECT_INTERFACE_VERSION
from syntra_build.domain.merges import (
    MERGE_INTERFACE_VERSION,
    MergeRequest,
    MergeResult,
    MergeStatus,
)
from syntra_build.domain.pull_requests import (
    PULL_REQUEST_INTERFACE_VERSION,
    PullRequestCreateRequest,
    PullRequestDescriptor,
    PullRequestState,
)
from syntra_build.domain.reviews import (
    ArchitectReview,
    ArchitectReviewRequest,
    ArchitectReviewVerdict,
)
from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.config.models import SecretInputs, SecretValue
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)
from syntra_build.infrastructure.persistence.codex import SQLiteCodexRunRepository

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
PROJECT = ProjectId(UUID(int=3216))
M1 = MilestoneId(UUID(int=3217))
M2 = MilestoneId(UUID(int=3218))
M1_STATES = [
    "READY",
    "PREPARING_TASK",
    "PREPARING_WORKSPACE",
    "CODING",
    "VALIDATING_CHANGES",
    "COMMITTING",
    "PUSHING",
    "PR_CREATING",
    "CI_RUNNING",
    "ARCHITECT_REVIEW",
    "MERGE_READY",
    "MERGING",
    "MERGE_VERIFY",
    "COMPLETE",
]
M1_JOBS = [
    "ARCHITECT_TASK",
    "WORKSPACE_PREPARE",
    "CODEX_RUN",
    "CHANGE_VALIDATE",
    "GIT_COMMIT",
    "GIT_PUSH",
    "PR_CREATE",
    "CI_RECONCILE",
    "ARCHITECT_REVIEW",
    "PR_MERGE",
]


def git(path: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


class FakeArchitect:
    provider_name = "fake"
    model = "deterministic"

    def __init__(self) -> None:
        self.task_requests: list[ArchitectTaskRequest] = []
        self.tasks: list[ArchitectTask] = []
        self.review_requests: list[ArchitectReviewRequest] = []

    def task(self, request: ArchitectTaskRequest) -> ArchitectTask:
        self.task_requests.append(request)
        if request.milestone_id == M1:
            task = ArchitectTask(
                ARCHITECT_TASK_INTERFACE_VERSION,
                request.correlation_id,
                request.project_id,
                request.milestone_id,
                ArchitectTaskType.IMPLEMENT,
                "Create generated.py with answer = 42",
                ("Create generated.py",),
                ("generated.py contains answer = 42",),
                (),
                ("pytest",),
                (),
            )
        else:
            assert request.milestone_id == M2
            task = ArchitectTask(
                ARCHITECT_TASK_INTERFACE_VERSION,
                request.correlation_id,
                request.project_id,
                request.milestone_id,
                ArchitectTaskType.IMPLEMENT,
                "Preserve generated.py with answer = 42 and add feature2.py",
                ("Preserve generated.py", "Create feature2.py"),
                ("feature2.py contains enabled = True",),
                (),
                ("pytest",),
                (),
            )
        self.tasks.append(task)
        return task

    def review(self, request: ArchitectReviewRequest) -> ArchitectReview:
        self.review_requests.append(request)
        return ArchitectReview(
            ARCHITECT_INTERFACE_VERSION,
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.pull_request_number,
            request.head_sha,
            ArchitectReviewVerdict.APPROVE,
            "Exact-head implementation approved",
            (),
        )

    def telemetry(self) -> dict[str, int | str | None]:
        return {"provider_response_id": "fake-architect-response"}

    def design(self, request: object) -> object:
        raise AssertionError("design is outside M32.18")

    def draft_specification(self, request: object) -> object:
        raise AssertionError("specification drafting is outside M32.18")


class FakeCodexProcess:
    saw_m1_output_for_m2 = False
    m2_initial_head: str | None = None

    def __init__(
        self, connection: sqlite3.Connection, trusted_git: TrustedGit, data_root: Path
    ) -> None:
        self.connection = connection
        self.trusted_git = trusted_git
        self.data_root = data_root

    def validate_workspace(
        self, request: CodexRunRequest, *, require_clean: bool
    ) -> None:
        BoundCodexRunner(
            WorkspaceService(self.connection, self.trusted_git, self.data_root), self
        ).validate_workspace(request, require_clean=require_clean)

    def run(
        self, request: CodexRunRequest, *, require_clean: bool = True
    ) -> CodexRunResult:
        runs = SQLiteCodexRunRepository(self.connection)
        runs.start(
            str(uuid4()),
            request,
            self.connection.execute(
                "SELECT id FROM git_workspaces WHERE milestone_id=?",
                (str(request.milestone_id),),
            ).fetchone()[0],
            "f" * 64,
            NOW,
            "fake-stdout",
            "fake-stderr",
        )
        if request.milestone_id == M1:
            (request.worktree_path / "generated.py").write_text("answer = 42\n")
        else:
            assert request.milestone_id == M2
            assert (
                request.worktree_path / "generated.py"
            ).read_text() == "answer = 42\n"
            type(self).saw_m1_output_for_m2 = True
            type(self).m2_initial_head = git(request.worktree_path, "rev-parse", "HEAD")
            (request.worktree_path / "feature2.py").write_text("enabled = True\n")
        result = CodexRunResult(
            request.interface_version,
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.job_id,
            request.attempt_number,
            CodexProcessStatus.SUCCEEDED,
            NOW,
            NOW,
            "fake-codex-process",
            request.timeout_seconds,
            exit_code=0,
        )
        run_id = self.connection.execute(
            "SELECT id FROM codex_runs WHERE job_id=?", (str(request.job_id),)
        ).fetchone()[0]
        runs.complete(run_id, result)
        return result

    def cancel(self, job_id: str, attempt_number: int) -> bool:
        raise AssertionError("cancellation is outside M32.18")


class FakeGitHub:
    def __init__(self, remote: Path) -> None:
        self.remote = remote
        self.pull_requests: dict[int, PullRequestDescriptor] = {}
        self.create_calls = 0
        self.merge_calls = 0
        self.before_merge: Callable[[MilestoneId], None] | None = None

    def _head(self, branch: str) -> str:
        return git(self.remote, "rev-parse", f"refs/heads/{branch}")

    def find_open(
        self,
        repository_full_name: str,
        head_branch: str,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> tuple[PullRequestDescriptor, ...]:
        return tuple(
            self._observed(pr)
            for pr in self.pull_requests.values()
            if pr.state is PullRequestState.OPEN
            and pr.head_branch == head_branch
            and pr.project_id == project_id
            and pr.milestone_id == milestone_id
        )

    def create(
        self, repository_full_name: str, request: PullRequestCreateRequest
    ) -> PullRequestDescriptor:
        self.create_calls += 1
        number = 15 + self.create_calls
        descriptor = PullRequestDescriptor(
            PULL_REQUEST_INTERFACE_VERSION,
            request.project_id,
            request.milestone_id,
            request.repository_id,
            number,
            PullRequestState.OPEN,
            request.head_branch,
            request.base_branch,
            request.head_sha,
            f"https://github.com/{repository_full_name}/pull/{number}",
        )
        self.pull_requests[number] = descriptor
        return descriptor

    def _observed(self, descriptor: PullRequestDescriptor) -> PullRequestDescriptor:
        if descriptor.state is PullRequestState.OPEN:
            descriptor = replace(
                descriptor, head_sha=self._head(descriptor.head_branch)
            )
            self.pull_requests[descriptor.pull_request_number] = descriptor
        return descriptor

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        descriptor = self.pull_requests[number]
        assert descriptor.project_id == project_id
        assert descriptor.milestone_id == milestone_id
        return self._observed(descriptor)

    def diff(
        self,
        repository_full_name: str,
        pull_request_number: int,
        expected_head_sha: str,
    ) -> str:
        descriptor = self._observed(self.pull_requests[pull_request_number])
        assert descriptor.head_sha == expected_head_sha
        filename, content = (
            ("generated.py", "answer = 42")
            if descriptor.milestone_id == M1
            else ("feature2.py", "enabled = True")
        )
        return f"diff --git a/{filename} b/{filename}\n+{content}"

    def merge(self, repository_full_name: str, request: MergeRequest) -> MergeResult:
        if self.before_merge is not None:
            self.before_merge(request.milestone_id)
        descriptor = self._observed(self.pull_requests[request.pull_request_number])
        assert descriptor.milestone_id == request.milestone_id
        assert descriptor.head_sha == request.expected_head_sha
        parent = self._head("main")
        tree = git(self.remote, "rev-parse", f"{descriptor.head_sha}^{{tree}}")
        identity = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Syntra acceptance",
            "GIT_AUTHOR_EMAIL": "syntra@example.test",
            "GIT_COMMITTER_NAME": "Syntra acceptance",
            "GIT_COMMITTER_EMAIL": "syntra@example.test",
            "GIT_AUTHOR_DATE": "2026-10-03T12:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-10-03T12:00:00+00:00",
        }
        merge_sha = git(
            self.remote,
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            f"Squash merge {request.milestone_id}",
            env=identity,
        )
        git(self.remote, "update-ref", "refs/heads/main", merge_sha, parent)
        self.merge_calls += 1
        descriptor = replace(
            descriptor,
            state=PullRequestState.MERGED,
            merged_at=NOW.isoformat(),
            merge_commit_sha=merge_sha,
        )
        self.pull_requests[descriptor.pull_request_number] = descriptor
        return MergeResult(
            MERGE_INTERFACE_VERSION,
            request.project_id,
            request.milestone_id,
            request.pull_request_number,
            MergeStatus.MERGED,
            merge_sha,
        )


class FakeActions:
    def __init__(self) -> None:
        self.observations: list[tuple[int, str]] = []

    def observe(
        self, repository_full_name: str, pull_request_number: int, head_sha: str
    ) -> CIObservation:
        self.observations.append((pull_request_number, head_sha))
        return CIObservation(
            (
                CICheck(
                    "tests",
                    "credential-free",
                    CICheckStatus.COMPLETED,
                    CICheckConclusion.PASSED,
                ),
            ),
            ("deterministic-run",),
        )

    def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
        raise AssertionError("passing CI must not rerun")


class LocalRemoteTrustedGit(TrustedGit):
    """Exercise real Git while translating the synthetic GitHub URL locally."""

    def __init__(self, authorised_root: Path, remote: Path) -> None:
        super().__init__(authorised_root)
        self.remote = str(remote)

    def ensure_bare(self, path: Path, remote_url: str) -> None:
        super().ensure_bare(path, self.remote)

    def fetch(self, path: Path, remote_url: str, branch: str) -> str:
        return super().fetch(path, self.remote, branch)

    def fetch_branch(self, repository: Path, remote_url: str, branch: str) -> str:
        return super().fetch_branch(repository, self.remote, branch)

    def remote_branch_sha(
        self, repository: Path, remote_url: str, branch: str
    ) -> str | None:
        return super().remote_branch_sha(repository, self.remote, branch)

    def push(self, repository: Path, remote_url: str, branch: str) -> None:
        super().push(repository, self.remote, branch)

    def origin(self, worktree: Path) -> str:
        return "https://github.com/owner/generated.git"


def seed(tmp_path: Path) -> tuple[Path, Path, Path, LocalRemoteTrustedGit, str]:
    data = tmp_path / "data"
    remote, source = tmp_path / "remote.git", tmp_path / "source"
    remote.mkdir()
    source.mkdir()
    git(remote, "init", "--bare")
    git(source, "init", "--initial-branch=main")
    (source / "README.md").write_text("# Generated\n")
    git(source, "add", ".")
    identity = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Syntra acceptance",
        "GIT_AUTHOR_EMAIL": "syntra@example.test",
        "GIT_COMMITTER_NAME": "Syntra acceptance",
        "GIT_COMMITTER_EMAIL": "syntra@example.test",
    }
    git(source, "commit", "-m", "approved baseline", env=identity)
    baseline_sha = git(source, "rev-parse", "HEAD")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "origin", "main")

    database = data / "syntra.db"
    data.mkdir()
    connection = open_database(database)
    apply_migrations(connection)
    SQLiteProjectRepository(connection, lambda: "project-transition").add(
        Project(PROJECT, "Happy path", ProjectState.BUILDING, NOW, NOW)
    )
    milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
    milestones.add(
        Milestone(
            M1, PROJECT, 0, "M1", "First artifact", MilestoneState.READY, NOW, NOW
        )
    )
    milestones.add(
        Milestone(
            M2, PROJECT, 1, "M2", "Second feature", MilestoneState.PENDING, NOW, NOW
        )
    )
    milestones.add_dependency(M2, M1)
    for milestone_id, objective, acceptance in (
        (M1, "Create generated.py", "The generated value is 42"),
        (M2, "Preserve generated.py and create feature2.py", "The feature is enabled"),
    ):
        connection.execute(
            "UPDATE milestones SET definition_json=?,automated_acceptance_json=? WHERE id=?",
            (
                json.dumps({"objective": objective}),
                json.dumps([acceptance]),
                str(milestone_id),
            ),
        )
    stamp = NOW.isoformat()
    spec, agents = "# Approved SPEC\n", "# Approved AGENTS\n"
    connection.execute(
        "INSERT INTO architect_requests(id,project_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,completed_at,status) VALUES('planning',?,'SPECIFICATION_DRAFT','fake','deterministic','high','1.0','{}','planning',?,?,'SUCCEEDED')",
        (str(PROJECT), stamp, stamp),
    )
    for document_id, kind, content in (
        ("00000000-0000-0000-0000-000000003201", "SPEC", spec),
        ("00000000-0000-0000-0000-000000003202", "AGENTS", agents),
    ):
        connection.execute(
            "INSERT INTO project_documents(id,project_id,document_type,revision,status,content,content_hash,created_at,created_by) VALUES(?,?,?,1,'DRAFT',?,?,?,'architect')",
            (
                document_id,
                str(PROJECT),
                kind,
                content,
                hashlib.sha256(content.encode()).hexdigest(),
                stamp,
            ),
        )
    connection.execute(
        "INSERT INTO human_gates(id,project_id,gate_type,state,title,prompt,expected_response_type,options_json,created_at,created_by,correlation_id) VALUES('00000000-0000-0000-0000-000000003203',?,'DESIGN_APPROVAL','RESOLVED','Approved','Approved?','DESIGN_APPROVAL','[]',?,'human','planning')",
        (str(PROJECT), stamp),
    )
    connection.execute(
        "INSERT INTO design_packages(id,project_id,architect_request_id,spec_document_id,agents_document_id,repository_visibility,design_summary,planned_milestones_json,assumptions_json,non_blocking_issues_json,status,approval_gate_id,created_at) VALUES('00000000-0000-0000-0000-000000003204',?,'planning','00000000-0000-0000-0000-000000003201','00000000-0000-0000-0000-000000003202','public','approved',?,'[]','[]','PENDING_APPROVAL','00000000-0000-0000-0000-000000003203',?)",
        (
            str(PROJECT),
            json.dumps(
                [
                    {"code": "M1", "title": "First artifact"},
                    {"code": "M2", "title": "Second feature"},
                ]
            ),
            stamp,
        ),
    )
    connection.execute(
        "UPDATE project_documents SET status='APPROVED',approved_at=?,approved_by='human'",
        (stamp,),
    )
    connection.execute(
        "UPDATE design_packages SET status='APPROVED',approved_at=?,approved_by='human' WHERE id='00000000-0000-0000-0000-000000003204'",
        (stamp,),
    )
    connection.execute(
        "INSERT INTO github_repositories(id,project_id,provider,owner,repository_name,full_name,external_repository_id,visibility,default_branch,status,created_at,updated_at,verified_at) VALUES('github',?,'github','owner','generated','owner/generated',3216,'public','main','VERIFIED',?,?,?)",
        (str(PROJECT), stamp, stamp, stamp),
    )
    connection.execute(
        "INSERT INTO repository_baselines(id,project_id,github_repository_id,commit_sha,spec_document_id,spec_revision,spec_content_hash,agents_document_id,agents_revision,agents_content_hash,created_at,verified_at) VALUES('baseline',?,'github',?,'00000000-0000-0000-0000-000000003201',1,?,'00000000-0000-0000-0000-000000003202',1,?,?,?)",
        (
            str(PROJECT),
            baseline_sha,
            hashlib.sha256(spec.encode()).hexdigest(),
            hashlib.sha256(agents.encode()).hexdigest(),
            stamp,
            stamp,
        ),
    )
    connection.commit()
    connection.close()
    return (
        database,
        data,
        remote,
        LocalRemoteTrustedGit(data / "auth", remote),
        baseline_sha,
    )


def config(tmp_path: Path, database: Path, data: Path):  # type: ignore[no-untyped-def]
    for path in (tmp_path / "app", tmp_path / "etc", tmp_path / "log"):
        path.mkdir()
    return load_config(
        {
            "filesystem": {
                "application_root": str(tmp_path / "app"),
                "configuration_root": str(tmp_path / "etc"),
                "data_root": str(data),
                "log_root": str(tmp_path / "log"),
                "workspace_root": str(data / "workspaces"),
                "backup_root": str(data / "backups"),
            },
            "database": {"sqlite_path": str(database)},
            "architect": {"enabled": True, "provider": "openai", "model": "fake"},
            "codex": {"executable": "/bin/true"},
            "github": {"enabled": True, "owner": "owner"},
            "metrics": {"enabled": False},
            "backups": {"enabled": False},
        },
        environ={},
        secrets=SecretInputs(
            architect_api_key=SecretValue("synthetic-architect-key"),
            github_token=SecretValue("synthetic-github-token"),
        ),
    )


def diagnostic(connection: sqlite3.Connection, remote: Path) -> str:
    tables = {
        "project": [
            dict(row) for row in connection.execute("SELECT id,state FROM projects")
        ],
        "milestones": [
            dict(row)
            for row in connection.execute(
                "SELECT id,code,state FROM milestones ORDER BY sequence_number"
            )
        ],
        "transitions": [
            dict(row)
            for row in connection.execute(
                "SELECT entity_type,entity_id,previous_state,new_state,reason FROM state_transitions ORDER BY rowid"
            )
        ],
        "jobs": [
            dict(row)
            for row in connection.execute(
                "SELECT milestone_id,job_type,state,attempt_number,last_error_id FROM jobs ORDER BY rowid"
            )
        ],
        "workspaces": [
            dict(row)
            for row in connection.execute(
                "SELECT milestone_id,branch_name,base_sha,current_head_sha FROM git_workspaces ORDER BY created_at"
            )
        ],
        "commits": [
            dict(row)
            for row in connection.execute(
                "SELECT milestone_id,commit_sha,parent_sha,pushed_at FROM commits ORDER BY committed_at"
            )
        ],
        "prs": [
            dict(row)
            for row in connection.execute(
                "SELECT milestone_id,external_pr_number,state,head_sha,merge_commit_sha FROM pull_requests ORDER BY external_pr_number"
            )
        ],
        "ci": [
            dict(row)
            for row in connection.execute(
                "SELECT pull_request_id,head_sha,overall_status FROM ci_runs ORDER BY started_at"
            )
        ],
        "reviews": [
            dict(row)
            for row in connection.execute(
                "SELECT milestone_id,pull_request_id,reviewed_sha,verdict FROM architect_reviews ORDER BY created_at"
            )
        ],
        "merges": [
            dict(row)
            for row in connection.execute(
                "SELECT milestone_id,expected_head_sha,status,merge_commit_sha FROM merge_attempts ORDER BY requested_at"
            )
        ],
        "remote": git(remote, "show-ref"),
    }
    return repr(tables)


def _states(
    connection: sqlite3.Connection, milestone: MilestoneId, initial: str
) -> list[str]:
    return [initial] + [
        row[0]
        for row in connection.execute(
            "SELECT new_state FROM state_transitions WHERE entity_type='MILESTONE' AND entity_id=? ORDER BY rowid",
            (str(milestone),),
        )
    ]


def test_two_milestones_progress_through_production_composition(tmp_path: Path) -> None:
    database, data, remote, trusted_git, b0 = seed(tmp_path)
    github, architect, actions = FakeGitHub(remote), FakeArchitect(), FakeActions()
    FakeCodexProcess.saw_m1_output_for_m2 = False
    FakeCodexProcess.m2_initial_head = None

    def runner(connection: sqlite3.Connection) -> FakeCodexProcess:
        return FakeCodexProcess(connection, trusted_git, data)

    dependencies = ProductionDependencies(
        architect_provider_factory=lambda: cast(ArchitectProvider, architect),
        codex_runner_factory=runner,
        trusted_git=trusted_git,
        github_pull_requests=github,
        github_actions_factory=lambda: actions,
    )
    executors = build_production_executors(
        config(tmp_path, database, data), dependencies=dependencies
    )
    assert {
        worker: dispatcher.job_types for worker, dispatcher in executors.items()
    } == dict(PRODUCTION_JOB_TYPES)
    assert "CI_RUNNING" not in STATE_JOBS

    control = open_database(database)
    assert control.execute("SELECT state FROM projects").fetchone()[0] == "BUILDING"
    assert [
        tuple(row)
        for row in control.execute(
            "SELECT code,state FROM milestones ORDER BY sequence_number"
        )
    ] == [("M1", "READY"), ("M2", "PENDING")]
    assert tuple(
        control.execute(
            "SELECT milestone_id,depends_on_milestone_id FROM milestone_dependencies"
        ).fetchone()
    ) == (str(M2), str(M1))

    merge_observations: dict[MilestoneId, dict[str, object]] = {}

    def observe_durable_merge_intent(milestone_id: MilestoneId) -> None:
        with open_database(database) as observation:
            attempt = observation.execute(
                "SELECT status,mutation_started_at,expected_head_sha,pull_request_id FROM merge_attempts WHERE milestone_id=?",
                (str(milestone_id),),
            ).fetchone()
            assert attempt is not None
            pull_request = observation.execute(
                "SELECT head_sha FROM pull_requests WHERE id=?",
                (attempt["pull_request_id"],),
            ).fetchone()
            assert attempt["status"] == "REQUESTED"
            assert attempt["mutation_started_at"] is not None
            assert attempt["expected_head_sha"] == pull_request["head_sha"]
            assert (
                observation.execute(
                    "SELECT state FROM milestones WHERE id=?", (str(milestone_id),)
                ).fetchone()[0]
                == "MERGING"
            )
            assert (
                observation.execute(
                    "SELECT COUNT(*) FROM ci_runs WHERE pull_request_id=? AND head_sha=? AND overall_status='PASSED'",
                    (attempt["pull_request_id"], attempt["expected_head_sha"]),
                ).fetchone()[0]
                == 1
            )
            assert (
                observation.execute(
                    "SELECT COUNT(*) FROM architect_reviews WHERE milestone_id=? AND pull_request_id=? AND reviewed_sha=? AND verdict='APPROVE' AND superseded_at IS NULL",
                    (
                        str(milestone_id),
                        attempt["pull_request_id"],
                        attempt["expected_head_sha"],
                    ),
                ).fetchone()[0]
                == 1
            )
            assert (
                observation.execute(
                    "SELECT COUNT(*) FROM architect_review_findings f JOIN architect_reviews r ON r.id=f.review_id WHERE r.milestone_id=? AND f.status='OPEN'",
                    (str(milestone_id),),
                ).fetchone()[0]
                == 0
            )
            merge_observations[milestone_id] = dict(attempt)

    github.before_merge = observe_durable_merge_intent
    due = ProductionDueWorkCoordinator(control, clock=lambda: NOW)
    scheduler = Scheduler(
        SQLiteJobRepository(control, lambda: str(uuid4())),
        WorkerCapacity({worker: 1 for worker in WorkerClass}),
        cast(Mapping[WorkerClass, JobExecutor], executors),
        clock=lambda: NOW,
        due_work_enqueuer=due.enqueue_due,
    )
    saw_m1_complete_before_promotion = False
    saw_m2_ready_before_start = False
    try:
        for _ in range(250):
            project_state = control.execute("SELECT state FROM projects").fetchone()[0]
            m1_state, m2_state = [
                row[0]
                for row in control.execute(
                    "SELECT state FROM milestones ORDER BY sequence_number"
                )
            ]
            if m1_state != "COMPLETE":
                assert m2_state == "PENDING"
                assert (
                    control.execute(
                        "SELECT COUNT(*) FROM jobs WHERE milestone_id=?", (str(M2),)
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    control.execute(
                        "SELECT COUNT(*) FROM git_workspaces WHERE milestone_id=?",
                        (str(M2),),
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    control.execute(
                        "SELECT COUNT(*) FROM pull_requests WHERE milestone_id=?",
                        (str(M2),),
                    ).fetchone()[0]
                    == 0
                )
            elif m2_state != "COMPLETE":
                assert project_state == "BUILDING"
                if m2_state == "PENDING":
                    saw_m1_complete_before_promotion = True
                if m2_state == "READY":
                    saw_m2_ready_before_start = True
                    assert (
                        control.execute(
                            "SELECT COUNT(*) FROM jobs WHERE milestone_id=?", (str(M2),)
                        ).fetchone()[0]
                        == 0
                    )
            if project_state == "COMPLETE":
                break
            scheduler.run_once()
            if scheduler.active_execution_count:
                scheduler.wait_for_wake(5)
        else:
            raise AssertionError(
                f"workflow did not complete: {diagnostic(control, remote)}"
            )
        scheduler.enter_drain()
        scheduler.run_once()

        assert saw_m1_complete_before_promotion
        assert saw_m2_ready_before_start
        assert _states(control, M1, "READY") == M1_STATES
        assert _states(control, M2, "PENDING") == ["PENDING", *M1_STATES]
        m2_promotion = control.execute(
            "SELECT reason,actor_type,actor_id FROM state_transitions WHERE entity_type='MILESTONE' AND entity_id=? AND previous_state='PENDING' AND new_state='READY'",
            (str(M2),),
        ).fetchone()
        assert tuple(m2_promotion) == ("dependencies complete", "SYSTEM", "lifecycle")

        project_transitions = [
            tuple(row)
            for row in control.execute(
                "SELECT previous_state,new_state,reason FROM state_transitions WHERE entity_type='PROJECT' AND entity_id=? ORDER BY rowid",
                (str(PROJECT),),
            )
        ]
        assert project_transitions == [
            ("BUILDING", "COMPLETING", "all milestones complete"),
            ("COMPLETING", "COMPLETE", "completion evidence verified"),
        ]
        assert (
            control.execute(
                "SELECT COUNT(*) FROM human_gates WHERE project_id=? AND state NOT IN ('RESOLVED','CANCELLED','EXPIRED')",
                (str(PROJECT),),
            ).fetchone()[0]
            == 0
        )
        assert (
            control.execute(
                "SELECT COUNT(*) FROM security_events e WHERE project_id=? AND blocking=1 AND severity IN ('HIGH','CRITICAL') AND NOT EXISTS (SELECT 1 FROM security_event_resolutions r WHERE r.security_event_id=e.id)",
                (str(PROJECT),),
            ).fetchone()[0]
            == 0
        )

        jobs = [
            tuple(row)
            for row in control.execute(
                "SELECT milestone_id,job_type,state,attempt_number FROM jobs ORDER BY rowid"
            )
        ]
        assert [job[1] for job in jobs] == M1_JOBS + M1_JOBS
        assert [job[0] for job in jobs] == [str(M1)] * 10 + [str(M2)] * 10
        assert {job[2] for job in jobs} == {"SUCCEEDED"}
        assert {job[3] for job in jobs} == {1}
        assert all(sum(job[1] == kind for job in jobs) == 2 for kind in M1_JOBS)
        assert all(job[1] != "CODEX_REVIEW_REWORK" for job in jobs)

        workspaces = control.execute(
            "SELECT id,milestone_id,branch_name,base_branch,base_sha,current_head_sha,worktree_path FROM git_workspaces ORDER BY created_at,id"
        ).fetchall()
        assert len(workspaces) == 2
        w1, w2 = workspaces
        assert w1["id"] != w2["id"]
        assert w1["branch_name"] == milestone_branch_name(0, "First artifact")
        assert w2["branch_name"] == milestone_branch_name(1, "Second feature")
        assert w1["branch_name"] != w2["branch_name"]
        assert w1["base_branch"] == w2["base_branch"] == "main"

        commits = {
            row["milestone_id"]: row
            for row in control.execute(
                "SELECT milestone_id,commit_sha,parent_sha,pushed_at FROM commits"
            )
        }
        h1, h2 = commits[str(M1)]["commit_sha"], commits[str(M2)]["commit_sha"]
        prs = {
            row["milestone_id"]: row
            for row in control.execute(
                "SELECT id,milestone_id,external_pr_number,state,head_branch,head_sha,merge_commit_sha FROM pull_requests"
            )
        }
        assert len(prs) == 2
        pr1, pr2 = prs[str(M1)], prs[str(M2)]
        s1, s2 = pr1["merge_commit_sha"], pr2["merge_commit_sha"]
        assert w1["base_sha"] == b0
        assert (
            commits[str(M1)]["parent_sha"] == b0 == git(remote, "rev-parse", f"{h1}^")
        )
        assert s1 != b0 and h1 != s1 and git(remote, "rev-parse", f"{s1}^") == b0
        assert w2["base_sha"] == s1 != b0
        assert FakeCodexProcess.m2_initial_head == s1
        assert (
            commits[str(M2)]["parent_sha"] == s1 == git(remote, "rev-parse", f"{h2}^")
        )
        assert s2 != s1 and h2 != s2 and git(remote, "rev-parse", f"{s2}^") == s1
        assert git(remote, "rev-parse", "refs/heads/main") == s2
        assert git(remote, "show", f"{s1}:generated.py") == "answer = 42"
        assert git(remote, "show", f"{s2}:generated.py") == "answer = 42"
        assert git(remote, "show", f"{s2}:feature2.py") == "enabled = True"
        assert FakeCodexProcess.saw_m1_output_for_m2
        assert git(remote, "rev-parse", f"refs/heads/{w1['branch_name']}") == h1
        assert git(remote, "rev-parse", f"refs/heads/{w2['branch_name']}") == h2
        assert w2["current_head_sha"] == h2

        assert pr1["external_pr_number"] == 16 and pr2["external_pr_number"] == 17
        assert pr1["state"] == pr2["state"] == "MERGED"
        assert pr1["head_branch"] == w1["branch_name"] and pr1["head_sha"] == h1
        assert pr2["head_branch"] == w2["branch_name"] and pr2["head_sha"] == h2
        active_prs = dict(
            control.execute("SELECT id,active_pull_request_id FROM milestones")
        )
        assert active_prs == {str(M1): pr1["id"], str(M2): pr2["id"]}

        ci = control.execute(
            "SELECT pull_request_id,head_sha,overall_status FROM ci_runs ORDER BY rowid"
        ).fetchall()
        reviews = control.execute(
            "SELECT milestone_id,pull_request_id,reviewed_sha,verdict FROM architect_reviews ORDER BY rowid"
        ).fetchall()
        attempts = control.execute(
            "SELECT milestone_id,pull_request_id,expected_head_sha,status,merge_commit_sha,mutation_started_at FROM merge_attempts ORDER BY rowid"
        ).fetchall()
        assert [tuple(row) for row in ci] == [
            (pr1["id"], h1, "PASSED"),
            (pr2["id"], h2, "PASSED"),
        ]
        assert actions.observations == [(16, h1), (17, h2)]
        assert [tuple(row) for row in reviews] == [
            (str(M1), pr1["id"], h1, "APPROVE"),
            (str(M2), pr2["id"], h2, "APPROVE"),
        ]
        assert [
            (
                row["milestone_id"],
                row["pull_request_id"],
                row["expected_head_sha"],
                row["status"],
                row["merge_commit_sha"],
            )
            for row in attempts
        ] == [
            (str(M1), pr1["id"], h1, "MERGED", s1),
            (str(M2), pr2["id"], h2, "MERGED", s2),
        ]
        assert all(row["mutation_started_at"] is not None for row in attempts)
        assert set(merge_observations) == {M1, M2}

        for milestone, expected_file in ((M1, "generated.py"), (M2, "feature2.py")):
            mid = str(milestone)
            assert (
                control.execute(
                    "SELECT COUNT(*) FROM git_workspaces WHERE milestone_id=?", (mid,)
                ).fetchone()[0]
                == 1
            )
            assert (
                control.execute(
                    "SELECT COUNT(*) FROM codex_runs WHERE milestone_id=? AND process_status='SUCCEEDED'",
                    (mid,),
                ).fetchone()[0]
                == 1
            )
            change_set = control.execute(
                "SELECT decision,files_json FROM change_sets WHERE milestone_id=?",
                (mid,),
            ).fetchone()
            assert change_set["decision"] == "ACCEPT"
            assert [item["path"] for item in json.loads(change_set["files_json"])] == [
                expected_file
            ]
            assert (
                control.execute(
                    "SELECT COUNT(*) FROM commits WHERE milestone_id=? AND pushed_at IS NOT NULL",
                    (mid,),
                ).fetchone()[0]
                == 1
            )
            assert (
                control.execute(
                    "SELECT COUNT(*) FROM pull_requests WHERE milestone_id=?", (mid,)
                ).fetchone()[0]
                == 1
            )
            assert (
                control.execute(
                    "SELECT COUNT(*) FROM architect_review_findings f JOIN architect_reviews r ON r.id=f.review_id WHERE r.milestone_id=? AND f.status='OPEN'",
                    (mid,),
                ).fetchone()[0]
                == 0
            )

        assert [request.milestone_id for request in architect.task_requests] == [M1, M2]
        assert [task.milestone_id for task in architect.tasks] == [M1, M2]
        assert [request.milestone_id for request in architect.review_requests] == [
            M1,
            M2,
        ]
        assert github.create_calls == github.merge_calls == 2
    finally:
        scheduler.close()
        control.close()
