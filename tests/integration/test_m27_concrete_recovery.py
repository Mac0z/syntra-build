# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import cast
from uuid import uuid4

from test_m22_pull_request_lifecycle import COMMIT, NOW, service
from test_m26_gatekeeper import MERGE_SHA, seed_eligible
from test_m26_gatekeeper import NOW as MERGE_NOW

from syntra_build.application.ci_handoff import PullRequestCIHandoff
from syntra_build.application.gatekeeper import Gatekeeper
from syntra_build.application.recovery import RecoveryCoordinator, RecoveryServices
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
    Scheduler,
    WorkerCapacity,
)
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain import Job, JobId, JobState, WorkerClass
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.merges import MergeStrategy
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.domain.workspaces import TrustedPushResult, WorkspaceError
from syntra_build.infrastructure.config import SchedulerConfig
from syntra_build.infrastructure.persistence import apply_migrations, open_database
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.recovery import SQLiteRecoveryRepository


def scheduler(db, executors=None):
    return Scheduler(
        SQLiteJobRepository(db, lambda: str(uuid4())),
        WorkerCapacity(SchedulerConfig().worker_class_limits()),
        executors or {},
    )


def test_pr_restart_adopts_remote_without_second_create(tmp_path: Path) -> None:
    with open_database(tmp_path / "pr.db") as db:
        apply_migrations(db)
        lifecycle, workspace, github, project, milestone = service(db)
        workspace.commit(
            project,
            milestone,
            "a" * 40,
            ["file.txt"],
            "accepted",
            NOW,
            expected_diff_hash="sha256:" + "1" * 64,
        )
        github.remote = [
            PullRequestDescriptor(
                "1.0",
                project,
                milestone,
                77,
                12,
                PullRequestState.OPEN,
                "syntra/m22",
                "main",
                COMMIT,
                "https://github.com/owner/repo/pull/12",
            )
        ]
        recovery_scheduler = scheduler(db)
        coordinator = RecoveryCoordinator(
            db,
            recovery_scheduler,
            services=RecoveryServices(
                pull_requests=PullRequestCIHandoff(db, lifecycle)
            ),
            clock=lambda: NOW + timedelta(seconds=1),
        )
        coordinator.recover(correlation_id="restart-pr")
        assert github.create_calls == 0
        assert workspace.commit_calls == 1
        assert db.execute("SELECT count(*) FROM pull_requests").fetchone()[0] == 1
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "CI_RUNNING"
        recovery_scheduler.close()


def test_it009_unknown_merge_is_observed_verified_without_second_put(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "merge.db") as db:
        apply_migrations(db)
        seed, github = seed_eligible(db)
        gatekeeper = Gatekeeper(db, github)
        attempt, _request = gatekeeper.prepare(
            seed.request, MergeStrategy.SQUASH, now=MERGE_NOW
        )
        db.execute(
            "UPDATE merge_attempts SET status='UNKNOWN',completed_at=? WHERE id=?",
            (MERGE_NOW.isoformat(), attempt),
        )
        github.live = replace(
            github.live,
            state=PullRequestState.MERGED,
            merge_commit_sha=MERGE_SHA,
            merged_at="2026-09-27T12:01:00Z",
        )
        github.get_calls.clear()
        recovery_scheduler = scheduler(db)
        coordinator = RecoveryCoordinator(
            db,
            recovery_scheduler,
            services=RecoveryServices(gatekeeper=gatekeeper),
            clock=lambda: MERGE_NOW + timedelta(minutes=2),
        )
        coordinator.recover(correlation_id="restart-merge")
        assert github.merge_calls == []
        assert len(github.get_calls) == 2
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "COMPLETE"
        assert db.execute("SELECT status FROM merge_attempts").fetchone()[0] == "MERGED"
        recovery_scheduler.close()


def test_unknown_open_merge_blocks_without_put(tmp_path: Path) -> None:
    with open_database(tmp_path / "unknown.db") as db:
        apply_migrations(db)
        seed, github = seed_eligible(db)
        gatekeeper = Gatekeeper(db, github)
        attempt, _ = gatekeeper.prepare(
            seed.request, MergeStrategy.SQUASH, now=MERGE_NOW
        )
        db.execute("UPDATE merge_attempts SET status='UNKNOWN' WHERE id=?", (attempt,))
        recovery_scheduler = scheduler(db)
        RecoveryCoordinator(
            db,
            recovery_scheduler,
            services=RecoveryServices(gatekeeper=gatekeeper),
            clock=lambda: MERGE_NOW + timedelta(seconds=1),
        ).recover(correlation_id="unknown-open")
        assert github.merge_calls == []
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
        assert db.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
        recovery_scheduler.close()


def test_merge_verify_restart_resumes_independent_verification(tmp_path: Path) -> None:
    with open_database(tmp_path / "merge-verify.db") as db:
        apply_migrations(db)
        seed, github = seed_eligible(db)
        gatekeeper = Gatekeeper(db, github)
        _attempt, _ = gatekeeper.prepare(
            seed.request, MergeStrategy.SQUASH, now=MERGE_NOW
        )
        db.execute(
            "UPDATE milestones SET state='MERGE_VERIFY' WHERE id=?",
            (str(seed.milestone),),
        )
        github.live = replace(
            github.live,
            state=PullRequestState.MERGED,
            merge_commit_sha=MERGE_SHA,
            merged_at="2026-09-27T12:01:00Z",
        )
        github.get_calls.clear()
        recovery_scheduler = scheduler(db)
        RecoveryCoordinator(
            db,
            recovery_scheduler,
            services=RecoveryServices(gatekeeper=gatekeeper),
            clock=lambda: MERGE_NOW + timedelta(minutes=2),
        ).recover(correlation_id="merge-verify")
        assert github.merge_calls == [] and len(github.get_calls) == 2
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "COMPLETE"
        recovery_scheduler.close()


def test_discovery_keeps_project_job_with_active_milestone(tmp_path: Path) -> None:
    with open_database(tmp_path / "discover.db") as db:
        apply_migrations(db)
        seed, _ = seed_eligible(db, milestone_state="CODING")
        jobs = SQLiteJobRepository(db, lambda: str(uuid4()))
        job = Job(
            JobId.generate(),
            seed.project,
            "PROJECT_JOB",
            JobState.RUNNING,
            0,
            NOW,
            NOW,
            worker_class=WorkerClass.INTERNAL,
        )
        jobs.add(job)
        subjects = SQLiteRecoveryRepository(db, lambda: str(uuid4())).discover()
        assert any(
            item.milestone_id == seed.milestone and item.job_id is None
            for item in subjects
        )
        assert any(
            item.milestone_id is None and item.job_id == job.id for item in subjects
        )


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.called = Event()

    def execute(self, job: Job) -> JobExecutionResult:
        self.calls += 1
        self.called.set()
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)


def test_scheduler_barrier_prevents_dispatch_until_ready(tmp_path: Path) -> None:
    with open_database(tmp_path / "barrier.db") as db:
        apply_migrations(db)
        seed, _ = seed_eligible(db, milestone_state="CODING")
        jobs = SQLiteJobRepository(db, lambda: str(uuid4()))
        queued = Job(
            JobId.generate(),
            seed.project,
            "QUEUED_CODEX",
            JobState.QUEUED,
            0,
            MERGE_NOW,
            MERGE_NOW,
            seed.milestone,
            worker_class=WorkerClass.CODEX,
        )
        jobs.add(queued)
        executor = RecordingExecutor()
        recovery_scheduler = scheduler(db, {WorkerClass.CODEX: executor})
        coordinator = RecoveryCoordinator(
            db, recovery_scheduler, clock=lambda: MERGE_NOW + timedelta(seconds=1)
        )
        coordinator.recover(correlation_id="barrier")
        assert executor.calls == 0
        assert coordinator.lifecycle.value == "READY"
        assert recovery_scheduler.run_once().dispatched == 1
        assert executor.called.wait(2)
        recovery_scheduler.wait_for_wake(2)
        recovery_scheduler.run_once()
        assert executor.calls == 1
        recovery_scheduler.close()


def test_restart_during_recovery_preserves_old_run_and_stays_drained(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "recovery-crash.db") as db:
        apply_migrations(db)
        seed_eligible(db, milestone_state="PR_CREATING")
        recovery_scheduler = scheduler(db)

        def crash(_subject, _correlation):
            raise KeyboardInterrupt

        first = RecoveryCoordinator(
            db,
            recovery_scheduler,
            handlers={"PR_CREATING": crash},
            clock=lambda: MERGE_NOW,
        )
        try:
            first.recover(correlation_id="same-correlation")
        except KeyboardInterrupt:
            pass
        assert recovery_scheduler.is_draining
        assert (
            db.execute("SELECT status FROM recovery_runs").fetchone()[0] == "RECOVERING"
        )
        second = RecoveryCoordinator(
            db,
            recovery_scheduler,
            clock=lambda: MERGE_NOW + timedelta(seconds=1),
        )
        second.recover(correlation_id="same-correlation")
        assert [
            row[0]
            for row in db.execute(
                "SELECT status FROM recovery_runs ORDER BY started_at"
            )
        ] == ["RECOVERING", "READY"]
        recovery_scheduler.close()


class RecoveryWorkspace:
    def __init__(self, delegate, *, remote: str, dirty: bool = False) -> None:
        self.delegate, self.remote, self.dirty = delegate, remote, dirty
        self.commit_calls = 0
        self.push_calls = 0

    def inspect(self, project, milestone, now):
        inspected = self.delegate.inspect(project, milestone, now)
        if not self.dirty:
            return inspected
        return inspected.__class__(
            inspected.workspace,
            inspected.origin,
            inspected.current_branch,
            inspected.head_sha,
            inspected.registered,
            ("dirty.txt",),
            (),
            (),
        )

    def push(self, project, milestone, expected, now):
        if self.remote == expected:
            return TrustedPushResult("syntra/m22", expected, self.remote)
        if self.remote != "a" * 40:
            raise WorkspaceError("unexpected remote SHA")
        self.push_calls += 1
        self.remote = expected
        return TrustedPushResult("syntra/m22", expected, expected)


def git_recovery_fixture(tmp_path: Path, state: str):
    db = open_database(tmp_path / f"git-{state}-{uuid4()}.db")
    apply_migrations(db)
    lifecycle, workspace, _github, project, milestone = service(db)
    workspace.commit(
        project,
        milestone,
        "a" * 40,
        ["file.txt"],
        "accepted",
        NOW,
        expected_diff_hash="sha256:" + "1" * 64,
    )
    db.execute("UPDATE milestones SET state=? WHERE id=?", (state, str(milestone)))
    return db, workspace, project, milestone


def test_committing_recovery_adopts_exact_commit_without_new_commit(
    tmp_path: Path,
) -> None:
    db, workspace, _project, _milestone = git_recovery_fixture(tmp_path, "COMMITTING")
    recovery_workspace = RecoveryWorkspace(workspace, remote="a" * 40)
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(workspace=cast(WorkspaceService, recovery_workspace)),
        clock=lambda: NOW,
    ).recover(correlation_id="commit")
    assert workspace.commit_calls == 1 and recovery_workspace.commit_calls == 0
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "PUSHING"
    recovery_scheduler.close()
    db.close()


def test_committing_dirty_workspace_blocks_without_commit(tmp_path: Path) -> None:
    db, workspace, _project, _milestone = git_recovery_fixture(tmp_path, "COMMITTING")
    recovery_workspace = RecoveryWorkspace(workspace, remote="a" * 40, dirty=True)
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(workspace=cast(WorkspaceService, recovery_workspace)),
        clock=lambda: NOW,
    ).recover(correlation_id="dirty-commit")
    assert workspace.commit_calls == 1 and recovery_workspace.commit_calls == 0
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
    assert db.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
    recovery_scheduler.close()
    db.close()


def test_pushing_recovery_is_idempotent_and_safe_retry_is_bounded(
    tmp_path: Path,
) -> None:
    db, workspace, _project, _milestone = git_recovery_fixture(tmp_path, "PUSHING")
    recovery_workspace = RecoveryWorkspace(workspace, remote="b" * 40)
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(workspace=cast(WorkspaceService, recovery_workspace)),
        clock=lambda: NOW,
    ).recover(correlation_id="already-pushed")
    assert recovery_workspace.push_calls == 0
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "PR_CREATING"
    recovery_scheduler.close()
    db.close()

    db, workspace, _project, _milestone = git_recovery_fixture(tmp_path, "PUSHING")
    recovery_workspace = RecoveryWorkspace(workspace, remote="a" * 40)
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(workspace=cast(WorkspaceService, recovery_workspace)),
        clock=lambda: NOW,
    ).recover(correlation_id="safe-push")
    assert recovery_workspace.push_calls == 1
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "PR_CREATING"
    recovery_scheduler.close()
    db.close()


def test_pushing_unexpected_remote_blocks_without_force_push(tmp_path: Path) -> None:
    db, workspace, _project, _milestone = git_recovery_fixture(tmp_path, "PUSHING")
    recovery_workspace = RecoveryWorkspace(workspace, remote="c" * 40)
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(workspace=cast(WorkspaceService, recovery_workspace)),
        clock=lambda: NOW,
    ).recover(correlation_id="unexpected-push")
    assert recovery_workspace.push_calls == 0
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
    assert db.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
    recovery_scheduler.close()
    db.close()


def test_recovery_blocks_only_ambiguous_project_and_recovers_sibling(
    tmp_path: Path,
) -> None:
    db = open_database(tmp_path / "isolation.db")
    apply_migrations(db)
    lifecycle, workspace, github, project_b, milestone_b = service(db)
    workspace.commit(
        project_b,
        milestone_b,
        "a" * 40,
        ["file.txt"],
        "accepted",
        NOW,
        expected_diff_hash="sha256:" + "1" * 64,
    )
    github.remote = [
        PullRequestDescriptor(
            "1.0",
            project_b,
            milestone_b,
            77,
            12,
            PullRequestState.OPEN,
            "syntra/m22",
            "main",
            "b" * 40,
            "https://github.com/owner/repo/pull/12",
        )
    ]
    project_a, milestone_a = ProjectId.generate(), MilestoneId.generate()
    stamp = NOW.isoformat()
    db.execute(
        """INSERT INTO projects
           (id,name,state,created_at,updated_at,last_state_change_at)
           VALUES (?,?,'BUILDING',?,?,?)""",
        (str(project_a), "ambiguous", stamp, stamp, stamp),
    )
    db.execute(
        """INSERT INTO milestones
           (id,project_id,sequence_number,code,title,state,created_at,updated_at)
           VALUES (?,?,1,'A','Ambiguous','COMMITTING',?,?)""",
        (str(milestone_a), str(project_a), stamp, stamp),
    )
    recovery_scheduler = scheduler(db)
    coordinator = RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(pull_requests=PullRequestCIHandoff(db, lifecycle)),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    coordinator.recover(correlation_id="isolation")
    assert coordinator.lifecycle.value == "READY"
    assert (
        db.execute(
            "SELECT state FROM projects WHERE id=?", (str(project_a),)
        ).fetchone()[0]
        == "BLOCKED"
    )
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(milestone_a),)
        ).fetchone()[0]
        == "BLOCKED"
    )
    assert (
        db.execute(
            "SELECT state FROM projects WHERE id=?", (str(project_b),)
        ).fetchone()[0]
        == "BUILDING"
    )
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(milestone_b),)
        ).fetchone()[0]
        == "CI_RUNNING"
    )
    recovery_scheduler.close()
    db.close()
