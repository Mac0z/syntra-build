# ruff: noqa: E501
"""M32.17 credential-free acceptance of one controlled review-rework cycle."""

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
from syntra_build.application.workspaces import WorkspaceService
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
    ReviewFinding,
    ReviewFindingSeverity,
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

NOW = datetime.now(UTC)
PROJECT = ProjectId(UUID(int=32170))
MILESTONE = MilestoneId(UUID(int=32171))
MERGE_SHA = "e" * 40
EXPECTED_STATES = [
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
    "REVIEW_REWORK",
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
EXPECTED_JOBS = [
    "ARCHITECT_TASK",
    "WORKSPACE_PREPARE",
    "CODEX_RUN",
    "CHANGE_VALIDATE",
    "GIT_COMMIT",
    "GIT_PUSH",
    "PR_CREATE",
    "CI_RECONCILE",
    "ARCHITECT_REVIEW",
    "CODEX_REVIEW_REWORK",
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


def finding_payload(row: sqlite3.Row) -> dict[str, str]:
    return {
        "finding_id": row["finding_code"],
        "severity": row["severity"],
        "requirement_ref": row["requirement_ref"],
        "description": row["description"],
        "recommended_action": row["recommended_action"],
    }


class FakeArchitect:
    provider_name = "fake"
    model = "deterministic"

    def __init__(self) -> None:
        self.requests: list[ArchitectReviewRequest] = []

    def task(self, request: ArchitectTaskRequest) -> ArchitectTask:
        return ArchitectTask(
            ARCHITECT_TASK_INTERFACE_VERSION,
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            ArchitectTaskType.IMPLEMENT,
            "Add the deterministic acceptance artifact",
            ("Create generated.py",),
            ("The generated value is 42",),
            (),
            ("pytest",),
            (),
        )

    def review(self, request: ArchitectReviewRequest) -> ArchitectReview:
        self.requests.append(request)
        first = len(self.requests) == 1
        assert request.ci_result["head_sha"] == request.head_sha
        assert ("answer = 41" if first else "answer = 42") in request.diff
        assert len(request.previous_findings) == (0 if first else 1)
        findings = (
            (
                ReviewFinding(
                    "VALUE_NOT_ACCEPTANCE_EXPECTATION",
                    ReviewFindingSeverity.MAJOR,
                    "The generated value is 42",
                    "generated.py sets answer to 41 rather than 42",
                    "change the value to 42",
                ),
            )
            if first
            else ()
        )
        return ArchitectReview(
            ARCHITECT_INTERFACE_VERSION,
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.pull_request_number,
            request.head_sha,
            (
                ArchitectReviewVerdict.CHANGES_REQUIRED
                if first
                else ArchitectReviewVerdict.APPROVE
            ),
            "Correction required" if first else "Exact-head correction approved",
            findings,
        )

    def telemetry(self) -> dict[str, int | str | None]:
        return {"provider_response_id": "fake-architect-response"}

    def design(self, request: object) -> object:
        raise AssertionError("design is outside M32.17")

    def draft_specification(self, request: object) -> object:
        raise AssertionError("specification drafting is outside M32.17")


class FakeCodexProcess:
    def __init__(
        self,
        connection: sqlite3.Connection,
        trusted_git: TrustedGit,
        data_root: Path,
        requests: list[CodexRunRequest],
    ) -> None:
        self.connection = connection
        self.trusted_git = trusted_git
        self.data_root = data_root
        self.requests = requests

    def validate_workspace(
        self, request: CodexRunRequest, *, require_clean: bool
    ) -> None:
        BoundCodexRunner(
            WorkspaceService(self.connection, self.trusted_git, self.data_root), self
        ).validate_workspace(request, require_clean=require_clean)

    def run(self, request: CodexRunRequest) -> CodexRunResult:
        self.requests.append(request)
        task_type = request.task["task_type"]
        if task_type == "REVIEW_REWORK":
            generated = "answer = 42\n"
        else:
            assert task_type == ArchitectTaskType.IMPLEMENT.value
            generated = "answer = 41\n"
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
        (request.worktree_path / "generated.py").write_text(generated)
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
        raise AssertionError("cancellation is outside M32.17")


class FakeGitHub:
    def __init__(self, head_supplier: Callable[[], str]) -> None:
        self.head_supplier = head_supplier
        self.live: PullRequestDescriptor | None = None
        self.create_calls = 0
        self.merge_calls = 0
        self.before_merge: Callable[[], None] | None = None
        self.created_head: str | None = None

    def find_open(
        self,
        repository_full_name: str,
        head_branch: str,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> tuple[PullRequestDescriptor, ...]:
        if self.live is not None and self.live.state is PullRequestState.OPEN:
            return (replace(self.live, head_sha=self.head_supplier()),)
        return ()

    def create(
        self, repository_full_name: str, request: PullRequestCreateRequest
    ) -> PullRequestDescriptor:
        self.create_calls += 1
        self.created_head = request.head_sha
        self.live = PullRequestDescriptor(
            PULL_REQUEST_INTERFACE_VERSION,
            request.project_id,
            request.milestone_id,
            request.repository_id,
            16,
            PullRequestState.OPEN,
            request.head_branch,
            request.base_branch,
            request.head_sha,
            f"https://github.com/{repository_full_name}/pull/16",
        )
        return self.live

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        assert self.live is not None
        if self.live.state is PullRequestState.OPEN:
            self.live = replace(self.live, head_sha=self.head_supplier())
        return self.live

    def diff(
        self,
        repository_full_name: str,
        pull_request_number: int,
        expected_head_sha: str,
    ) -> str:
        value = 41 if expected_head_sha == self.created_head else 42
        return f"diff --git a/generated.py b/generated.py\n+answer = {value}"

    def merge(self, repository_full_name: str, request: MergeRequest) -> MergeResult:
        if self.before_merge is not None:
            self.before_merge()
        self.merge_calls += 1
        assert self.live is not None
        self.live = replace(
            self.live,
            state=PullRequestState.MERGED,
            merged_at=NOW.isoformat(),
            merge_commit_sha=MERGE_SHA,
        )
        return MergeResult(
            MERGE_INTERFACE_VERSION,
            request.project_id,
            request.milestone_id,
            request.pull_request_number,
            MergeStatus.MERGED,
            MERGE_SHA,
        )


class FakeActions:
    def observe(
        self, repository_full_name: str, pull_request_number: int, head_sha: str
    ) -> CIObservation:
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


def seed(tmp_path: Path) -> tuple[Path, Path, TrustedGit]:
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
        Project(PROJECT, "Controlled rework", ProjectState.BUILDING, NOW, NOW)
    )
    SQLiteMilestoneRepository(connection, lambda: "milestone-transition").add(
        Milestone(
            MILESTONE,
            PROJECT,
            0,
            "M1",
            "Controlled rework",
            MilestoneState.READY,
            NOW,
            NOW,
        )
    )
    connection.execute(
        "UPDATE milestones SET definition_json=?,automated_acceptance_json=? WHERE id=?",
        (
            json.dumps({"objective": "Create generated.py"}),
            json.dumps(["The generated value is 42"]),
            str(MILESTONE),
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
            json.dumps([{"code": "M1", "title": "Controlled rework"}]),
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
    return database, data, LocalRemoteTrustedGit(data / "auth", remote)


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


def diagnostic(connection: sqlite3.Connection) -> str:
    def rows(query: str) -> list[dict[str, object]]:
        return [dict(row) for row in connection.execute(query)]

    return repr(
        {
            "project": connection.execute("SELECT state FROM projects").fetchone()[0],
            "milestone": connection.execute("SELECT state FROM milestones").fetchone()[
                0
            ],
            "jobs": rows(
                "SELECT job_type,state,attempt_number,failure_classification,last_error_id FROM jobs ORDER BY rowid"
            ),
            "pull_requests": rows(
                "SELECT id,state,head_sha FROM pull_requests ORDER BY rowid"
            ),
            "ci_runs": rows(
                "SELECT id,pull_request_id,head_sha,overall_status FROM ci_runs ORDER BY rowid"
            ),
            "reviews": rows(
                "SELECT id,pull_request_id,reviewed_sha,verdict FROM architect_reviews ORDER BY rowid"
            ),
            "findings": rows(
                "SELECT review_id,finding_code,status FROM architect_review_findings ORDER BY rowid"
            ),
            "rework_tasks": rows(
                "SELECT id,review_id,pull_request_id FROM architect_rework_tasks ORDER BY rowid"
            ),
            "change_sets": rows(
                "SELECT id,head_sha_before_commit,diff_hash,decision FROM change_sets ORDER BY rowid"
            ),
            "commits": rows(
                "SELECT commit_sha,parent_sha,change_set_id,pushed_at FROM commits ORDER BY rowid"
            ),
        }
    )


def test_controlled_review_rework_path_through_production_composition(
    tmp_path: Path,
) -> None:
    database, data, trusted_git = seed(tmp_path)
    codex_requests: list[CodexRunRequest] = []

    def remote_milestone_head() -> str:
        heads = git(
            Path(cast(LocalRemoteTrustedGit, trusted_git).remote),
            "for-each-ref",
            "--format=%(objectname)",
            "refs/heads/syntra/*",
        ).splitlines()
        assert len(heads) == 1
        return heads[0]

    github = FakeGitHub(remote_milestone_head)
    architect, actions = FakeArchitect(), FakeActions()

    def runner(connection: sqlite3.Connection) -> FakeCodexProcess:
        return FakeCodexProcess(connection, trusted_git, data, codex_requests)

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
    merge_precondition_observed = False

    def observe_durable_merge_intent() -> None:
        nonlocal merge_precondition_observed
        with open_database(database) as observation:
            attempt = observation.execute(
                """SELECT status,mutation_started_at,expected_head_sha
                   FROM merge_attempts"""
            ).fetchone()
            pull_request = observation.execute(
                "SELECT head_sha FROM pull_requests"
            ).fetchone()
            assert attempt is not None
            assert pull_request is not None
            assert attempt["status"] == "REQUESTED"
            assert attempt["mutation_started_at"] is not None
            assert attempt["expected_head_sha"] == pull_request["head_sha"]
            assert (
                observation.execute(
                    """SELECT COUNT(*) FROM ci_runs
                   WHERE pull_request_id=(SELECT id FROM pull_requests)
                     AND head_sha=? AND overall_status='PASSED'""",
                    (attempt["expected_head_sha"],),
                ).fetchone()[0]
                == 1
            )
            assert (
                observation.execute(
                    """SELECT COUNT(*) FROM architect_reviews
                   WHERE pull_request_id=(SELECT id FROM pull_requests)
                     AND reviewed_sha=? AND verdict='APPROVE'
                     AND superseded_at IS NULL""",
                    (attempt["expected_head_sha"],),
                ).fetchone()[0]
                == 1
            )
            assert (
                observation.execute(
                    "SELECT COUNT(*) FROM architect_review_findings WHERE status='OPEN'"
                ).fetchone()[0]
                == 0
            )
            assert (
                observation.execute(
                    "SELECT state FROM milestones WHERE id=?", (str(MILESTONE),)
                ).fetchone()[0]
                == "MERGING"
            )
        merge_precondition_observed = True

    github.before_merge = observe_durable_merge_intent
    due = ProductionDueWorkCoordinator(control, clock=lambda: datetime.now(UTC))
    scheduler = Scheduler(
        SQLiteJobRepository(control, lambda: str(uuid4())),
        WorkerCapacity({worker: 1 for worker in WorkerClass}),
        cast(Mapping[WorkerClass, JobExecutor], executors),
        clock=lambda: datetime.now(UTC),
        due_work_enqueuer=due.enqueue_due,
    )
    try:
        for _ in range(150):
            if (
                control.execute(
                    "SELECT state FROM milestones WHERE id=?", (str(MILESTONE),)
                ).fetchone()[0]
                == "COMPLETE"
            ):
                break
            scheduler.run_once()
            if scheduler.active_execution_count:
                scheduler.wait_for_wake(5)
        else:
            raise AssertionError(f"workflow did not complete: {diagnostic(control)}")
        scheduler.enter_drain()
        scheduler.run_once()  # harvest the terminal PR_MERGE job without new work

        states = ["READY"] + [
            row[0]
            for row in control.execute(
                "SELECT new_state FROM state_transitions WHERE entity_type='MILESTONE' AND entity_id=? ORDER BY rowid",
                (str(MILESTONE),),
            )
        ]
        jobs = [
            tuple(row)
            for row in control.execute("SELECT job_type,state FROM jobs ORDER BY rowid")
        ]
        assert states == EXPECTED_STATES
        assert [item[0] for item in jobs] == EXPECTED_JOBS
        assert {item[1] for item in jobs} == {"SUCCEEDED"}
        job_types = [item[0] for item in jobs]
        assert job_types.count("CODEX_RUN") == 1
        assert job_types.count("CODEX_REVIEW_REWORK") == 1
        assert job_types.count("CHANGE_VALIDATE") == 2
        assert job_types.count("GIT_COMMIT") == 2
        assert job_types.count("GIT_PUSH") == 2
        assert job_types.count("PR_CREATE") == 2
        assert job_types.count("CI_RECONCILE") == 2
        assert job_types.count("ARCHITECT_REVIEW") == 2
        assert job_types.count("PR_MERGE") == 1
        rework_job = control.execute(
            "SELECT id,worker_class,payload_json FROM jobs WHERE job_type='CODEX_REVIEW_REWORK'"
        ).fetchone()
        assert rework_job is not None
        assert rework_job["worker_class"] == WorkerClass.CODEX.value
        assert set(json.loads(rework_job["payload_json"])) == {
            "task_type",
            "review_id",
            "rework_task_id",
            "pull_request_id",
        }
        assert control.execute("SELECT state FROM projects").fetchone()[0] == "BUILDING"
        pull_request = control.execute("SELECT * FROM pull_requests").fetchone()
        assert pull_request is not None
        assert pull_request["state"] == "MERGED"
        h2 = pull_request["head_sha"]
        assert pull_request["merge_commit_sha"] == MERGE_SHA
        assert pull_request["external_pr_number"] == 16
        assert pull_request["head_branch"].startswith("syntra/")
        assert pull_request["base_branch"] == "main"
        assert control.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0] == 1

        commits = control.execute("SELECT * FROM commits ORDER BY rowid").fetchall()
        assert len(commits) == 2
        h1 = commits[0]["commit_sha"]
        assert h1 != h2
        assert commits[1]["commit_sha"] == h2
        assert commits[1]["parent_sha"] == h1
        assert all(row["pushed_at"] is not None for row in commits)
        assert remote_milestone_head() == h2

        change_sets = control.execute(
            "SELECT * FROM change_sets ORDER BY rowid"
        ).fetchall()
        assert len(change_sets) == 2
        assert {row["decision"] for row in change_sets} == {"ACCEPT"}
        assert change_sets[0]["diff_hash"] != change_sets[1]["diff_hash"]
        assert change_sets[1]["head_sha_before_commit"] == h1
        assert commits[0]["change_set_id"] == change_sets[0]["id"]
        assert commits[1]["change_set_id"] == change_sets[1]["id"]
        assert all(
            [item["path"] for item in json.loads(row["files_json"])] == ["generated.py"]
            for row in change_sets
        )

        ci_runs = control.execute("SELECT * FROM ci_runs ORDER BY rowid").fetchall()
        assert len(ci_runs) == 2
        assert [row["head_sha"] for row in ci_runs] == [h1, h2]
        assert {row["overall_status"] for row in ci_runs} == {"PASSED"}
        assert {row["pull_request_id"] for row in ci_runs} == {pull_request["id"]}

        reviews = control.execute(
            "SELECT * FROM architect_reviews ORDER BY rowid"
        ).fetchall()
        assert len(reviews) == 2
        first_review, second_review = reviews
        assert (first_review["reviewed_sha"], first_review["verdict"]) == (
            h1,
            "CHANGES_REQUIRED",
        )
        assert (second_review["reviewed_sha"], second_review["verdict"]) == (
            h2,
            "APPROVE",
        )
        assert second_review["superseded_at"] is None
        finding = control.execute("SELECT * FROM architect_review_findings").fetchone()
        assert finding is not None
        assert finding["finding_code"] == "VALUE_NOT_ACCEPTANCE_EXPECTATION"
        assert finding["severity"] == "major"
        assert finding["status"] == "RESOLVED"
        assert finding["resolved_by_review_id"] == second_review["id"]
        assert (
            control.execute(
                "SELECT COUNT(*) FROM architect_review_findings WHERE status='OPEN'"
            ).fetchone()[0]
            == 0
        )

        rework_task = control.execute("SELECT * FROM architect_rework_tasks").fetchone()
        assert rework_task is not None
        task_payload = json.loads(rework_task["task_payload_json"])
        assert rework_task["review_id"] == first_review["id"]
        assert rework_task["pull_request_id"] == pull_request["id"]
        assert task_payload["reviewed_sha"] == h1
        assert task_payload["branch"] == pull_request["head_branch"]
        assert task_payload["findings"] == [finding_payload(finding)]
        assert task_payload["agents_instructions"]["content"] == "# Approved AGENTS\n"

        assert len(codex_requests) == 2
        initial_request, rework_request = codex_requests
        assert initial_request.task["task_type"] == ArchitectTaskType.IMPLEMENT.value
        assert rework_request.task == task_payload
        assert initial_request.job_id != rework_request.job_id
        assert rework_request.agents_markdown == "# Approved AGENTS\n"
        codex_runs = control.execute(
            """SELECT r.job_id,r.process_status,j.job_type
               FROM codex_runs r JOIN jobs j ON j.id=r.job_id ORDER BY r.rowid"""
        ).fetchall()
        assert len(codex_runs) == 2
        assert {row["process_status"] for row in codex_runs} == {"SUCCEEDED"}
        assert [row["job_type"] for row in codex_runs] == [
            "CODEX_RUN",
            "CODEX_REVIEW_REWORK",
        ]

        assert len(architect.requests) == 2
        assert architect.requests[0].head_sha == h1
        assert architect.requests[0].ci_result["head_sha"] == h1
        assert architect.requests[0].previous_findings == ()
        assert architect.requests[1].pull_request_number == 16
        assert architect.requests[1].head_sha == h2
        assert architect.requests[1].ci_result["head_sha"] == h2
        assert architect.requests[1].previous_findings == (
            ReviewFinding(
                "VALUE_NOT_ACCEPTANCE_EXPECTATION",
                ReviewFindingSeverity.MAJOR,
                "The generated value is 42",
                "generated.py sets answer to 41 rather than 42",
                "change the value to 42",
            ),
        )

        merge_attempt = control.execute(
            """SELECT project_id,milestone_id,pull_request_id,expected_head_sha,
                      status,merge_commit_sha,mutation_started_at
               FROM merge_attempts"""
        ).fetchone()
        assert merge_attempt is not None
        assert merge_attempt["project_id"] == str(PROJECT)
        assert merge_attempt["milestone_id"] == str(MILESTONE)
        assert merge_attempt["pull_request_id"] == pull_request["id"]
        assert merge_attempt["expected_head_sha"] == pull_request["head_sha"]
        assert merge_attempt["status"] == "MERGED"
        assert merge_attempt["merge_commit_sha"] == MERGE_SHA
        assert merge_attempt["mutation_started_at"] is not None
        assert github.create_calls == 1
        assert merge_precondition_observed
        assert github.merge_calls == 1
        milestone = control.execute(
            "SELECT state,architect_rework_count FROM milestones"
        ).fetchone()
        assert tuple(milestone) == ("COMPLETE", 1)
    finally:
        scheduler.close()
        control.close()
