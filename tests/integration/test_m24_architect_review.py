# ruff: noqa: E501
from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build.application.architect import ArchitectError, ArchitectFailureKind
from syntra_build.application.architect_review import (
    ArchitectApprovalFreshness,
    ArchitectReviewError,
    ArchitectReviewService,
)
from syntra_build.application.ci_handoff import PullRequestCIHandoff
from syntra_build.application.review_rework import ReviewReworkCodexExecutor
from syntra_build.application.scheduler import Scheduler, WorkerCapacity
from syntra_build.domain.codex import (
    CodexProcessStatus,
    CodexRunRequest,
    CodexRunResult,
)
from syntra_build.domain.design import ARCHITECT_INTERFACE_VERSION
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.jobs import WorkerClass
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.domain.reviews import (
    ArchitectReview,
    ArchitectReviewRequest,
    ArchitectReviewVerdict,
    ReviewFinding,
    ReviewFindingSeverity,
)
from syntra_build.infrastructure.persistence.connection import (
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.migrations import apply_migrations
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)

NOW = datetime(2026, 9, 23, tzinfo=UTC)
SHA_A, SHA_B = "a" * 40, "b" * 40


class GitHubFake:
    def __init__(self, descriptor: PullRequestDescriptor) -> None:
        self.descriptor = descriptor
        self.after_call: PullRequestDescriptor | None = None
        self.get_count = 0

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        self.get_count += 1
        if self.after_call is not None and self.get_count >= 3:
            return self.after_call
        return self.descriptor

    def diff(
        self,
        repository_full_name: str,
        pull_request_number: int,
        expected_head_sha: str,
    ) -> str:
        return f"diff --git a/file b/file\n+{expected_head_sha}"


class ArchitectFake:
    provider_name = "fake"
    model = "architect-test"

    def __init__(
        self,
        verdict: ArchitectReviewVerdict,
        *,
        mismatch: str | None = None,
        fail: bool = False,
    ) -> None:
        self.verdict, self.mismatch, self.fail = verdict, mismatch, fail
        self.requests: list[ArchitectReviewRequest] = []

    def review(self, request: ArchitectReviewRequest) -> ArchitectReview:
        self.requests.append(request)
        if self.fail:
            raise ArchitectError(ArchitectFailureKind.TRANSIENT_PROVIDER, "offline")
        project, milestone, number, sha = (
            request.project_id,
            request.milestone_id,
            request.pull_request_number,
            request.head_sha,
        )
        if self.mismatch == "project":
            project = ProjectId.generate()
        elif self.mismatch == "milestone":
            milestone = MilestoneId.generate()
        elif self.mismatch == "pr":
            number += 1
        elif self.mismatch == "sha":
            sha = SHA_B
        findings: tuple[ReviewFinding, ...] = ()
        if self.verdict is ArchitectReviewVerdict.CHANGES_REQUIRED:
            findings = (
                ReviewFinding(
                    "finding-1",
                    ReviewFindingSeverity.MINOR,
                    "M24.1",
                    "Fix exact-head handling",
                    "Use persisted identity",
                ),
            )
        return ArchitectReview(
            ARCHITECT_INTERFACE_VERSION,
            request.correlation_id,
            project,
            milestone,
            number,
            sha,
            self.verdict,
            "reviewed",
            findings,
        )

    def telemetry(self) -> dict[str, int | str | None]:
        return {"provider_response_id": "response-1"}


def seeded(
    tmp_path: Path,
    *,
    state: MilestoneState = MilestoneState.ARCHITECT_REVIEW,
    ci_sha: str | None = SHA_A,
    documents: bool = True,
) -> tuple[sqlite3.Connection, ProjectId, MilestoneId, str, PullRequestDescriptor]:
    connection = open_database(tmp_path / f"{uuid4()}.db")
    apply_migrations(connection)
    project, milestone, repository_id, pr_id = (
        ProjectId.generate(),
        MilestoneId.generate(),
        str(uuid4()),
        str(uuid4()),
    )
    stamp = NOW.isoformat()
    connection.execute(
        """INSERT INTO projects (id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility) VALUES (?,?,'BUILDING',?,?,?,?, 'public')""",
        (str(project), "M24", stamp, stamp, stamp, f"m24-{uuid4().hex}"),
    )
    connection.execute(
        """INSERT INTO milestones (id,project_id,sequence_number,code,title,state,created_at,updated_at,definition_json,automated_acceptance_json) VALUES (?,?,24,'M24','Review',?,?,?,'{}','[]')""",
        (str(milestone), str(project), state.value, stamp, stamp),
    )
    connection.execute(
        """INSERT INTO github_repositories (id,project_id,provider,owner,repository_name,full_name,external_repository_id,visibility,default_branch,status,created_at,updated_at,verified_at) VALUES (?,?,'github','owner','repo','owner/repo',77,'public','main','VERIFIED',?,?,?)""",
        (repository_id, str(project), stamp, stamp, stamp),
    )
    descriptor = PullRequestDescriptor(
        "1.0",
        project,
        milestone,
        77,
        32,
        PullRequestState.OPEN,
        "syntra/m24",
        "main",
        SHA_A,
        "https://github.com/owner/repo/pull/32",
    )
    with transaction(connection):
        pr = SQLitePullRequestRepository(connection).save_verified(
            pr_id, repository_id, descriptor, "M24", NOW
        )
    if ci_sha is not None:
        connection.execute(
            """INSERT INTO ci_runs (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,overall_status,started_at,completed_at,last_checked_at,summary_json,retry_count) VALUES (?,?,?,?,?,1,'PASSED',?,?,?,'{}',0)""",
            (
                str(uuid4()),
                str(project),
                str(milestone),
                pr.id,
                ci_sha,
                stamp,
                stamp,
                stamp,
            ),
        )
    if documents:
        for kind, content in (
            ("SPEC", "# approved spec"),
            ("AGENTS", "# approved agents"),
        ):
            connection.execute(
                """INSERT INTO project_documents (id,project_id,document_type,revision,status,content,content_hash,created_at,created_by,approved_at,approved_by) VALUES (?,?,?,1,'APPROVED',?,?,?,'human',?,'human')""",
                (str(uuid4()), str(project), kind, content, "f" * 64, stamp, stamp),
            )
    return connection, project, milestone, pr.id, descriptor


def service(
    connection: sqlite3.Connection, github: GitHubFake, provider: ArchitectFake
) -> ArchitectReviewService:
    return ArchitectReviewService(
        connection, github, github, provider, clock=lambda: NOW
    )


def test_review_preconditions_fail_closed(tmp_path: Path) -> None:
    db, project, milestone, _, descriptor = seeded(
        tmp_path, state=MilestoneState.CI_RUNNING
    )
    with db, pytest.raises(ArchitectReviewError, match="ARCHITECT_REVIEW"):
        service(
            db, GitHubFake(descriptor), ArchitectFake(ArchitectReviewVerdict.APPROVE)
        ).review(project, milestone, "wrong-state")
    db, project, milestone, _, descriptor = seeded(tmp_path, ci_sha=None)
    with db, pytest.raises(ArchitectReviewError, match="passing CI"):
        service(
            db, GitHubFake(descriptor), ArchitectFake(ArchitectReviewVerdict.APPROVE)
        ).review(project, milestone, "no-ci")
    db, project, milestone, _, descriptor = seeded(tmp_path, ci_sha=SHA_B)
    with db, pytest.raises(ArchitectReviewError, match="passing CI"):
        service(
            db, GitHubFake(descriptor), ArchitectFake(ArchitectReviewVerdict.APPROVE)
        ).review(project, milestone, "stale-ci")
    db, project, milestone, _, descriptor = seeded(tmp_path, documents=False)
    with db, pytest.raises(ArchitectReviewError, match="SPEC and AGENTS"):
        service(
            db, GitHubFake(descriptor), ArchitectFake(ArchitectReviewVerdict.APPROVE)
        ).review(project, milestone, "no-docs")


def test_live_pr_and_response_identity_mismatches_are_rejected(tmp_path: Path) -> None:
    db, project, milestone, _, descriptor = seeded(tmp_path)
    wrong = PullRequestDescriptor(
        "1.0",
        project,
        milestone,
        999,
        32,
        PullRequestState.OPEN,
        "syntra/m24",
        "main",
        SHA_A,
        descriptor.web_url,
    )
    with db, pytest.raises(ArchitectReviewError, match="identity"):
        service(
            db, GitHubFake(wrong), ArchitectFake(ArchitectReviewVerdict.APPROVE)
        ).review(project, milestone, "bad-live")
    for mismatch in ("project", "milestone", "pr", "sha"):
        db, project, milestone, _, descriptor = seeded(tmp_path)
        with db, pytest.raises(ArchitectReviewError, match="response was invalid"):
            service(
                db,
                GitHubFake(descriptor),
                ArchitectFake(ArchitectReviewVerdict.APPROVE, mismatch=mismatch),
            ).review(project, milestone, f"bad-{mismatch}")
        assert db.execute("SELECT count(*) FROM architect_reviews").fetchone()[0] == 0


def test_approve_exact_head_and_toctou_staleness(tmp_path: Path) -> None:
    db, project, milestone, pr_id, descriptor = seeded(tmp_path)
    with db:
        result = service(
            db, GitHubFake(descriptor), ArchitectFake(ArchitectReviewVerdict.APPROVE)
        ).review(project, milestone, "approve")
        assert (
            SQLiteMilestoneRepository(db, lambda: str(uuid4()))
            .get(milestone, project)
            .state
            is MilestoneState.MERGE_READY
        )
        assert ArchitectApprovalFreshness(db).is_current(
            project, milestone, pr_id, SHA_A
        )
        assert not ArchitectApprovalFreshness(db).is_current(
            project, milestone, pr_id, SHA_B
        )
        assert result.superseded_at is None

    db, project, milestone, pr_id, descriptor = seeded(tmp_path)
    github = GitHubFake(descriptor)
    github.after_call = PullRequestDescriptor(
        "1.0",
        project,
        milestone,
        77,
        32,
        PullRequestState.OPEN,
        "syntra/m24",
        "main",
        SHA_B,
        descriptor.web_url,
    )
    with db:
        result = service(
            db, github, ArchitectFake(ArchitectReviewVerdict.APPROVE)
        ).review(project, milestone, "toctou")
        assert result.superseded_at is not None
        assert (
            SQLiteMilestoneRepository(db, lambda: str(uuid4()))
            .get(milestone, project)
            .state
            is MilestoneState.BLOCKED
        )
        assert not ArchitectApprovalFreshness(db).is_current(
            project, milestone, pr_id, SHA_B
        )
        assert not ArchitectApprovalFreshness(db).is_current(
            project, milestone, pr_id, SHA_A
        )
        review = db.execute(
            "SELECT reviewed_sha,superseded_at FROM architect_reviews WHERE id=?",
            (result.id,),
        ).fetchone()
        assert review["reviewed_sha"] == SHA_A and review["superseded_at"] is not None
        transition = db.execute(
            """SELECT reason,metadata_json FROM state_transitions
            WHERE milestone_id=? AND new_state='BLOCKED' ORDER BY rowid DESC LIMIT 1""",
            (str(milestone),),
        ).fetchone()
        metadata = json.loads(transition["metadata_json"])
        assert transition["reason"] == (
            "PR head changed unexpectedly during Architect review"
        )
        assert metadata == {
            "newly_observed_sha": SHA_B,
            "pull_request_id": pr_id,
            "review_id": result.id,
            "stale_reviewed_sha": SHA_A,
        }
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE job_type='CI_RECONCILE'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                """SELECT count(*) FROM state_transitions
                WHERE milestone_id=? AND new_state='MERGE_READY'""",
                (str(milestone),),
            ).fetchone()[0]
            == 0
        )


def test_changes_required_persists_findings_and_exactly_one_codex_job(
    tmp_path: Path,
) -> None:
    db, project, milestone, pr_id, descriptor = seeded(tmp_path)
    reviewer = service(
        db,
        GitHubFake(descriptor),
        ArchitectFake(ArchitectReviewVerdict.CHANGES_REQUIRED),
    )
    with db:
        result = reviewer.review(project, milestone, "changes")
        assert (
            db.execute(
                "SELECT count(*) FROM architect_review_findings WHERE review_id=? AND status='OPEN'",
                (result.id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT count(*) FROM architect_rework_tasks WHERE review_id=?",
                (result.id,),
            ).fetchone()[0]
            == 1
        )
        jobs = db.execute(
            "SELECT * FROM jobs WHERE job_type='CODEX_REVIEW_REWORK'"
        ).fetchall()
        assert len(jobs) == 1 and jobs[0]["worker_class"] == "CODEX"
        assert json.loads(jobs[0]["payload_json"])["pull_request_id"] == pr_id
        assert (
            SQLiteMilestoneRepository(db, lambda: str(uuid4()))
            .get(milestone, project)
            .state
            is MilestoneState.CODING
        )
        history = [
            row[0]
            for row in db.execute(
                "SELECT new_state FROM state_transitions WHERE milestone_id=? ORDER BY rowid",
                (str(milestone),),
            )
        ]
        assert history[-2:] == ["REVIEW_REWORK", "CODING"]
        again = reviewer.review(project, milestone, "changes")
        assert again.id == result.id
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE job_type='CODEX_REVIEW_REWORK'"
            ).fetchone()[0]
            == 1
        )


def test_provider_failure_is_audited_without_successful_review(tmp_path: Path) -> None:
    db, project, milestone, _, descriptor = seeded(tmp_path)
    with db, pytest.raises(ArchitectError):
        service(
            db,
            GitHubFake(descriptor),
            ArchitectFake(ArchitectReviewVerdict.APPROVE, fail=True),
        ).review(project, milestone, "failure")
    assert db.execute("SELECT status FROM architect_requests").fetchone()[0] == "FAILED"
    assert db.execute("SELECT count(*) FROM architect_reviews").fetchone()[0] == 0


def test_durable_job_runs_through_scheduler_codex_boundary(tmp_path: Path) -> None:
    db, project, milestone, _, descriptor = seeded(tmp_path)
    service(
        db,
        GitHubFake(descriptor),
        ArchitectFake(ArchitectReviewVerdict.CHANGES_REQUIRED),
    ).review(project, milestone, "scheduled-rework")
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    repository_id, workspace_id = str(uuid4()), str(uuid4())
    db.execute(
        """INSERT INTO git_repositories (id,project_id,github_repository_id,repository_path,remote_name,remote_url,default_branch,created_at,updated_at) VALUES (?,?,(SELECT id FROM github_repositories WHERE project_id=?),?,'origin','https://github.com/owner/repo.git','main',?,?)""",
        (
            repository_id,
            str(project),
            str(project),
            str(tmp_path / "repo.git"),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    db.execute(
        """INSERT INTO git_workspaces (id,project_id,milestone_id,git_repository_id,branch_name,worktree_path,base_branch,base_sha,current_head_sha,state,created_at) VALUES (?,?,?,?,?,?, 'main',?,?,'READY',?)""",
        (
            workspace_id,
            str(project),
            str(milestone),
            repository_id,
            "syntra/m24",
            str(workspace),
            SHA_A,
            SHA_A,
            NOW.isoformat(),
        ),
    )
    database_path = Path(db.execute("PRAGMA database_list").fetchone()[2])

    class Runner:
        def run(self, request: CodexRunRequest) -> CodexRunResult:
            assert request.task["task_type"] == "REVIEW_REWORK"
            assert "github" not in request.task
            return CodexRunResult(
                "1.0",
                request.correlation_id,
                request.project_id,
                request.milestone_id,
                request.job_id,
                request.attempt_number,
                CodexProcessStatus.SUCCEEDED,
                NOW,
                NOW,
                "fake-codex",
                request.timeout_seconds,
                exit_code=0,
            )

        def cancel(self, job_id: str, attempt_number: int) -> bool:
            return False

    executor = ReviewReworkCodexExecutor(
        database_path,
        lambda _connection: Runner(),
        timeout_seconds=60,
        clock=lambda: NOW,
    )
    capacities = WorkerCapacity(dict.fromkeys(WorkerClass, 1))
    scheduler = Scheduler(
        SQLiteJobRepository(db, lambda: str(uuid4())),
        capacities,
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW,
    )
    try:
        assert scheduler.run_once().dispatched == 1
        for _ in range(100):
            if scheduler.run_once().completed:
                break
            time.sleep(0.01)
        else:
            pytest.fail("Codex scheduler job did not complete")
        assert (
            SQLiteMilestoneRepository(db, lambda: str(uuid4()))
            .get(milestone, project)
            .state
            is MilestoneState.VALIDATING_CHANGES
        )
        assert (
            db.execute(
                "SELECT state FROM jobs WHERE job_type='CODEX_REVIEW_REWORK'"
            ).fetchone()[0]
            == "SUCCEEDED"
        )
    finally:
        scheduler.close()


def test_open_minor_finding_prevents_current_approval(tmp_path: Path) -> None:
    db, project, milestone, pr_id, descriptor = seeded(tmp_path)
    with db:
        service(
            db, GitHubFake(descriptor), ArchitectFake(ArchitectReviewVerdict.APPROVE)
        ).review(project, milestone, "approve-minor")
        review_id = db.execute("SELECT id FROM architect_reviews").fetchone()[0]
        db.execute(
            """INSERT INTO architect_review_findings (id,review_id,finding_code,severity,requirement_ref,description,recommended_action,status,created_at) VALUES (?,?,?,'minor','M24','still open','resolve it','OPEN',?)""",
            (str(uuid4()), review_id, "minor-open", NOW.isoformat()),
        )
        assert not ArchitectApprovalFreshness(db).is_current(
            project, milestone, pr_id, SHA_A
        )


def transition(
    db: sqlite3.Connection,
    project: ProjectId,
    milestone: MilestoneId,
    previous: MilestoneState,
    target: MilestoneState,
    correlation: str,
) -> None:
    SQLiteMilestoneRepository(db, lambda: str(uuid4())).apply_transition(
        MilestoneTransitionRequest(
            milestone,
            project,
            previous,
            target,
            "integration progression",
            "SYSTEM",
            "test",
            correlation,
            NOW,
        )
    )


def test_cross_component_same_pr_rework_to_second_approval(tmp_path: Path) -> None:
    db, project, milestone, pr_id, descriptor_a = seeded(tmp_path)
    github = GitHubFake(descriptor_a)
    with db:
        first = service(
            db, github, ArchitectFake(ArchitectReviewVerdict.CHANGES_REQUIRED)
        ).review(project, milestone, "first-review")
        assert (
            db.execute(
                "SELECT count(*) FROM architect_review_findings WHERE review_id=?",
                (first.id,),
            ).fetchone()[0]
            == 1
        )
        transition(
            db,
            project,
            milestone,
            MilestoneState.CODING,
            MilestoneState.VALIDATING_CHANGES,
            "validated",
        )
        transition(
            db,
            project,
            milestone,
            MilestoneState.VALIDATING_CHANGES,
            MilestoneState.COMMITTING,
            "committed",
        )
        transition(
            db,
            project,
            milestone,
            MilestoneState.COMMITTING,
            MilestoneState.PUSHING,
            "pushed",
        )
        descriptor_b = PullRequestDescriptor(
            "1.0",
            project,
            milestone,
            77,
            32,
            PullRequestState.OPEN,
            "syntra/m24",
            "main",
            SHA_B,
            descriptor_a.web_url,
        )
        with transaction(db):
            record_b = SQLitePullRequestRepository(db).save_verified(
                pr_id,
                db.execute(
                    "SELECT github_repository_id FROM pull_requests WHERE id=?",
                    (pr_id,),
                ).fetchone()[0],
                descriptor_b,
                "M24",
                NOW,
            )
        PullRequestCIHandoff(db, object()).accept_verified(record_b, "same-pr", NOW)  # type: ignore[arg-type]
        assert (
            db.execute(
                "SELECT count(*) FROM pull_requests WHERE milestone_id=?",
                (str(milestone),),
            ).fetchone()[0]
            == 1
        )
        assert not ArchitectApprovalFreshness(db).is_current(
            project, milestone, pr_id, SHA_B
        )
        db.execute(
            """INSERT INTO ci_runs (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,overall_status,started_at,completed_at,last_checked_at,summary_json,retry_count) VALUES (?,?,?,?,?,1,'PASSED',?,?,?,'{}',0)""",
            (
                str(uuid4()),
                str(project),
                str(milestone),
                pr_id,
                SHA_B,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        transition(
            db,
            project,
            milestone,
            MilestoneState.CI_RUNNING,
            MilestoneState.ARCHITECT_REVIEW,
            "ci-b-passed",
        )
        github.descriptor, github.after_call, github.get_count = descriptor_b, None, 0
        second = service(
            db, github, ArchitectFake(ArchitectReviewVerdict.APPROVE)
        ).review(project, milestone, "second-review")
        assert second.reviewed_sha == SHA_B
        assert ArchitectApprovalFreshness(db).is_current(
            project, milestone, pr_id, SHA_B
        )
        assert (
            SQLiteMilestoneRepository(db, lambda: str(uuid4()))
            .get(milestone, project)
            .state
            is MilestoneState.MERGE_READY
        )
        assert db.execute("SELECT count(*) FROM architect_reviews").fetchone()[0] == 2
