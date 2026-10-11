"""Offline regression coverage for bounded, exact-head CI infrastructure waits."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build.adapters.github.actions import CIProviderError, CIProviderFailure
from syntra_build.application.ci_monitor import CIMonitor, CIMonitorError
from syntra_build.application.ci_polling import (
    AdaptivePollingPolicy,
    InfrastructureRetryPolicy,
    MissingCIPolicy,
)
from syntra_build.application.ci_scheduler import CIJobCoordinator
from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIDiagnosis,
    CIFailureClassification,
    CIObservation,
    CIOverallStatus,
    CIWorkflowPresence,
)
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.project_state_machine import ProjectTransitionRequest
from syntra_build.domain.projects import ProjectState
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.infrastructure.persistence.ci import CIRunRecord, SQLiteCIRepository
from syntra_build.infrastructure.persistence.connection import (
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.migrations import apply_migrations
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)

HEAD = "a" * 40
NEW_HEAD = "b" * 40


@dataclass
class FrozenClock:
    now: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class PullRequests:
    def __init__(self) -> None:
        self.head = HEAD
        self.calls = 0
        self.change_after_read: str | None = None
        self.observed_project: ProjectId | None = None
        self.observed_milestone: MilestoneId | None = None
        self.on_get: Callable[[], None] | None = None

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        self.calls += 1
        descriptor = PullRequestDescriptor(
            "1.0",
            self.observed_project or project_id,
            self.observed_milestone or milestone_id,
            77 if repository_full_name == "owner/one" else 78,
            number,
            PullRequestState.OPEN,
            "syntra/m23",
            "main",
            self.head,
            f"https://example/pr/{number}",
        )
        if self.change_after_read is not None:
            self.head, self.change_after_read = self.change_after_read, None
        if self.on_get is not None:
            self.on_get()
        return descriptor

    def find_open(self, *args: object) -> tuple[()]:
        return ()

    def create(self, *args: object) -> PullRequestDescriptor:
        raise AssertionError("CI monitoring must not create a pull request")


class Actions:
    def __init__(self) -> None:
        self.observation = CIObservation((), (), CIWorkflowPresence.CONFIGURED)
        self.failure: CIProviderFailure | None = None
        self.calls: list[tuple[str, int, str]] = []
        self.on_observe: Callable[[], None] | None = None

    def observe(
        self, repository_full_name: str, pull_request_number: int, head_sha: str
    ) -> CIObservation:
        self.calls.append((repository_full_name, pull_request_number, head_sha))
        if self.on_observe is not None:
            self.on_observe()
        if self.failure is not None:
            raise CIProviderError(self.failure, "injected GitHub API failure")
        return self.observation

    def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
        raise AssertionError("missing CI infrastructure must never trigger a rerun")


class CancelActions(Actions):
    def __init__(self) -> None:
        super().__init__()
        self.rerun_calls: list[tuple[str, str]] = []
        self.observation = CIObservation(
            (
                CICheck(
                    "validate",
                    "cancelled-job",
                    CICheckStatus.COMPLETED,
                    CICheckConclusion.CANCELLED,
                ),
            ),
            ("actual-run",),
        )

    def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
        self.rerun_calls.append((repository_full_name, workflow_run_id))


def _seed(
    connection: sqlite3.Connection, clock: FrozenClock, name: str = "one"
) -> tuple[ProjectId, MilestoneId, str]:
    project, milestone = ProjectId.generate(), MilestoneId.generate()
    repository = str(uuid4())
    now = clock().isoformat()
    connection.execute(
        """INSERT INTO projects
        (id,name,state,created_at,updated_at,last_state_change_at,
         canonical_name,repository_visibility)
        VALUES (?,?,'BUILDING',?,?,?,?,'public')""",
        (str(project), name, now, now, now, name),
    )
    connection.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,started_at,
         created_at,updated_at)
        VALUES (?,?,23,'M23','CI','CI_RUNNING',?,?,?)""",
        (str(milestone), str(project), now, now, now),
    )
    connection.execute(
        """INSERT INTO github_repositories VALUES
        (?,?,'github','owner',?,?,?,'public','main','VERIFIED',?,?,?)""",
        (
            repository,
            str(project),
            name,
            f"owner/{name}",
            77 if name == "one" else 78,
            now,
            now,
            now,
        ),
    )
    descriptor = PullRequests().get(f"owner/{name}", 12, project, milestone)
    with transaction(connection):
        pr = SQLitePullRequestRepository(connection).save_verified(
            str(uuid4()), repository, descriptor, "M23", clock()
        )
    return project, milestone, pr.id


@dataclass
class Harness:
    connection: sqlite3.Connection
    project: ProjectId
    milestone: MilestoneId
    pr_id: str
    clock: FrozenClock
    prs: PullRequests
    actions: Actions

    def monitor(self, policy: MissingCIPolicy | None = None) -> CIMonitor:
        return CIMonitor(
            self.connection,
            self.prs,
            self.actions,
            clock=self.clock,
            missing_ci=policy or MissingCIPolicy(grace_seconds=10, max_observations=20),
            polling=AdaptivePollingPolicy(initial_seconds=1),
            retries=InfrastructureRetryPolicy((1, 2)),
        )

    def reconcile(self, policy: MissingCIPolicy | None = None) -> CIRunRecord:
        return self.monitor(policy).reconcile(
            self.project, self.milestone, "missing-ci-regression"
        )

    def state(self) -> str:
        row = self.connection.execute(
            "SELECT state FROM milestones WHERE id=?", (str(self.milestone),)
        ).fetchone()
        return str(row["state"])

    def set_project_state(self, target: ProjectState) -> None:
        projects = SQLiteProjectRepository(self.connection, lambda: str(uuid4()))
        projects.apply_transition(
            ProjectTransitionRequest(
                self.project,
                projects.get(self.project).state,
                target,
                "injected human pause/resume",
                "HUMAN",
                "tester",
                "pause-regression",
                self.clock(),
            )
        )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    with closing(open_database(tmp_path / "ci.db")) as connection:
        apply_migrations(connection)
        clock = FrozenClock()
        project, milestone, pr_id = _seed(connection, clock)
        yield Harness(
            connection, project, milestone, pr_id, clock, PullRequests(), Actions()
        )


def _assert_infrastructure_block(harness: Harness, record: CIRunRecord) -> None:
    assert record.overall_status is CIOverallStatus.UNKNOWN
    assert record.next_check_at is None
    assert record.diagnosis is not None and record.diagnosis_summary
    assert harness.state() == "BLOCKED"
    history = harness.connection.execute(
        """SELECT * FROM state_transitions WHERE milestone_id=?
        AND new_state='BLOCKED'""",
        (str(harness.milestone),),
    ).fetchall()
    assert len(history) == 1
    assert "PR #12" in history[0]["reason"]
    assert record.head_sha in history[0]["reason"]
    metadata = json.loads(history[0]["metadata_json"])
    assert metadata["ci_run_id"] == record.id
    assert metadata["head_sha"] == record.head_sha
    assert metadata["diagnosis"] == record.diagnosis.value
    assert (
        harness.connection.execute(
            """SELECT count(*) FROM state_transitions WHERE milestone_id=?
        AND new_state IN ('CI_REWORK','ARCHITECT_REVIEW')""",
            (str(harness.milestone),),
        ).fetchone()[0]
        == 0
    )
    assert (
        harness.connection.execute(
            "SELECT count(*) FROM jobs WHERE worker_class='CODEX'"
        ).fetchone()[0]
        == 0
    )


def _check_observation(
    status: CICheckStatus, conclusion: CICheckConclusion | None = None
) -> CIObservation:
    return CIObservation(
        (CICheck("validate", "job-1", status, conclusion),), ("run-1",)
    )


def test_no_runs_starts_exact_head_grace_without_rework(harness: Harness) -> None:
    record = harness.reconcile()
    assert record.head_sha == HEAD and record.pull_request_id == harness.pr_id
    assert record.overall_status is CIOverallStatus.UNKNOWN
    assert record.checks == () and record.external_workflow_run_ids == ()
    assert record.unknown_since == harness.clock()
    assert record.unknown_deadline_at == harness.clock() + timedelta(seconds=10)
    assert record.unknown_observation_count == 1
    assert record.diagnosis is CIDiagnosis.NO_APPLICABLE_RUN
    assert record.failure_classification is None and record.retry_count == 0
    assert record.next_check_at is not None
    assert harness.clock() < record.next_check_at <= record.unknown_deadline_at
    assert harness.state() == "CI_RUNNING"
    assert harness.actions.calls == [("owner/one", 12, HEAD)]


@pytest.mark.parametrize(
    ("observation", "diagnosis"),
    [
        (
            CIObservation((), (), CIWorkflowPresence.MISSING),
            CIDiagnosis.MISSING_WORKFLOW,
        ),
        (
            CIObservation((), (), CIWorkflowPresence.CONFIGURED),
            CIDiagnosis.NO_APPLICABLE_RUN,
        ),
        (
            CIObservation((), ("run-1",), CIWorkflowPresence.CONFIGURED),
            CIDiagnosis.NO_REQUIRED_CHECKS,
        ),
        (
            _check_observation(CICheckStatus.COMPLETED, CICheckConclusion.SKIPPED),
            CIDiagnosis.UNRESOLVED_CHECKS,
        ),
    ],
)
def test_grace_expiry_durably_diagnoses_absence_or_unresolved_checks(
    harness: Harness, observation: CIObservation, diagnosis: CIDiagnosis
) -> None:
    harness.actions.observation = observation
    waiting = harness.reconcile()
    assert waiting.diagnosis is diagnosis
    harness.clock.advance(10)
    blocked = harness.reconcile()
    assert blocked.id == waiting.id and blocked.diagnosis is diagnosis
    assert blocked.unknown_since == waiting.unknown_since
    assert blocked.unknown_deadline_at == waiting.unknown_deadline_at
    assert blocked.unknown_observation_count == 2
    assert blocked.retry_count == 0
    _assert_infrastructure_block(harness, blocked)
    assert CIJobCoordinator(harness.connection).enqueue_due(harness.clock()) == 0


def test_delayed_runs_clear_grace_and_fresh_pass_enters_architect_review(
    harness: Harness,
) -> None:
    waiting = harness.reconcile()
    harness.clock.advance(5)
    harness.actions.observation = _check_observation(CICheckStatus.RUNNING)
    running = harness.reconcile()
    assert running.id == waiting.id
    assert running.overall_status is CIOverallStatus.RUNNING
    assert running.unknown_since is None and running.unknown_deadline_at is None
    assert running.unknown_observation_count == 0 and running.diagnosis is None
    assert harness.state() == "CI_RUNNING"
    harness.clock.advance(100)
    harness.actions.observation = _check_observation(
        CICheckStatus.COMPLETED, CICheckConclusion.PASSED
    )
    passed = harness.reconcile()
    assert passed.id == waiting.id and passed.head_sha == HEAD
    assert passed.overall_status is CIOverallStatus.PASSED
    assert harness.state() == "ARCHITECT_REVIEW"
    assert passed.next_check_at is None


@pytest.mark.parametrize("status", [CIOverallStatus.QUEUED, CIOverallStatus.RUNNING])
def test_actual_jobless_active_run_is_polled_without_missing_ci_expiry(
    harness: Harness, status: CIOverallStatus
) -> None:
    harness.reconcile()
    harness.clock.advance(30)
    harness.actions.observation = CIObservation(
        (), ("actual-run",), CIWorkflowPresence.CONFIGURED, status
    )
    running = harness.reconcile()
    assert running.overall_status is status and running.next_check_at is not None
    assert running.unknown_since is None and running.unknown_deadline_at is None
    assert running.unknown_observation_count == 0 and running.diagnosis is None
    assert harness.state() == "CI_RUNNING"


def test_observation_limit_bounds_wait_before_wall_clock_deadline(
    harness: Harness,
) -> None:
    policy = MissingCIPolicy(grace_seconds=600, max_observations=3)
    first = harness.reconcile(policy)
    replay = harness.reconcile(policy)
    assert replay.id == first.id and replay.unknown_observation_count == 1
    harness.clock.advance(1)
    assert harness.reconcile(policy).unknown_observation_count == 2
    harness.clock.advance(1)
    blocked = harness.reconcile(policy)
    assert blocked.unknown_observation_count == 3
    assert blocked.unknown_deadline_at == first.unknown_deadline_at
    assert harness.clock() < policy.deadline_at(first.started_at)
    _assert_infrastructure_block(harness, blocked)


@pytest.mark.parametrize(
    ("failure", "diagnosis"),
    [
        (CIProviderFailure.AUTHENTICATION, CIDiagnosis.PROVIDER_AUTHENTICATION),
        (CIProviderFailure.REJECTION, CIDiagnosis.PROVIDER_REJECTION),
        (CIProviderFailure.MALFORMED, CIDiagnosis.PROVIDER_MALFORMED),
        (CIProviderFailure.TRANSIENT, CIDiagnosis.PROVIDER_UNAVAILABLE),
    ],
)
def test_api_failures_keep_separate_retry_budget_and_preserve_unknown_grace(
    harness: Harness, failure: CIProviderFailure, diagnosis: CIDiagnosis
) -> None:
    waiting = harness.reconcile()
    harness.actions.failure = failure
    # An API failure after expiry is still provider failure, never proof of absence.
    harness.clock.advance(30)
    failed = harness.reconcile()
    assert failed.id == waiting.id and failed.diagnosis is diagnosis
    assert failed.retry_count == 1 and failed.next_check_at is not None
    assert failed.unknown_since == waiting.unknown_since
    assert failed.unknown_deadline_at == waiting.unknown_deadline_at
    assert failed.unknown_observation_count == waiting.unknown_observation_count
    assert harness.state() == "CI_RUNNING"
    assert failed.failure_classification is (
        CIFailureClassification.TRANSIENT_INFRASTRUCTURE
        if failure is CIProviderFailure.TRANSIENT
        else CIFailureClassification.UNKNOWN
    )
    for _ in range(2):
        harness.clock.advance(2)
        failed = harness.reconcile()
    assert failed.retry_count == 3 and failed.diagnosis is diagnosis
    _assert_infrastructure_block(harness, failed)


def test_provider_outage_without_successful_observation_does_not_start_grace(
    harness: Harness,
) -> None:
    harness.actions.failure = CIProviderFailure.TRANSIENT
    record = harness.reconcile()
    assert record.diagnosis is CIDiagnosis.PROVIDER_UNAVAILABLE
    assert record.unknown_since is None and record.unknown_deadline_at is None
    assert record.unknown_observation_count == 0
    assert (
        record.failure_classification
        is CIFailureClassification.TRANSIENT_INFRASTRUCTURE
    )
    assert record.retry_count == 1 and harness.state() == "CI_RUNNING"


def test_successful_no_runs_after_provider_recovery_keeps_original_grace(
    harness: Harness,
) -> None:
    original = harness.reconcile()
    harness.clock.advance(1)
    harness.actions.failure = CIProviderFailure.TRANSIENT
    failed = harness.reconcile()
    assert failed.unknown_observation_count == 1
    harness.actions.failure = None
    harness.clock.advance(1)
    recovered = harness.reconcile()
    assert recovered.diagnosis is CIDiagnosis.NO_APPLICABLE_RUN
    assert recovered.unknown_since == original.unknown_since
    assert recovered.unknown_deadline_at == original.unknown_deadline_at
    assert recovered.unknown_observation_count == 2
    assert harness.state() == "CI_RUNNING"


def test_provider_failure_replay_at_same_clock_is_idempotent(harness: Harness) -> None:
    harness.actions.failure = CIProviderFailure.TRANSIENT
    first = harness.reconcile()
    repeated = harness.reconcile()
    assert first == repeated and repeated.retry_count == 1
    assert harness.state() == "CI_RUNNING"
    assert harness.connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 1
    harness.clock.advance(1)
    assert harness.reconcile().retry_count == 2


def test_successful_unknown_after_api_error_at_same_clock_counts_once(
    harness: Harness,
) -> None:
    first = harness.reconcile()
    harness.clock.advance(1)
    harness.actions.failure = CIProviderFailure.TRANSIENT
    failed = harness.reconcile()
    assert failed.unknown_observation_count == 1
    harness.actions.failure = None
    recovered = harness.reconcile()
    replay = harness.reconcile()
    assert recovered.unknown_observation_count == replay.unknown_observation_count == 2
    assert recovered.unknown_since == first.unknown_since
    assert recovered.unknown_deadline_at == first.unknown_deadline_at
    assert recovered.diagnosis is CIDiagnosis.NO_APPLICABLE_RUN
    assert recovered == replay and harness.state() == "CI_RUNNING"


def test_real_failed_tests_enter_rework_instead_of_infrastructure_block(
    harness: Harness,
) -> None:
    harness.reconcile()
    harness.clock.advance(20)
    harness.actions.observation = CIObservation(
        (
            CICheck(
                "validate",
                "job-1",
                CICheckStatus.COMPLETED,
                CICheckConclusion.FAILED,
                failure_summary="Run unit tests",
            ),
        ),
        ("run-1",),
    )
    failed = harness.reconcile()
    assert failed.overall_status is CIOverallStatus.FAILED
    assert failed.failure_classification is CIFailureClassification.TEST
    assert failed.unknown_since is None and failed.diagnosis is None
    assert failed.next_check_at is None and harness.state() == "CI_REWORK"


def test_cancelled_actual_run_replays_until_due_then_reruns_and_passes(
    harness: Harness,
) -> None:
    actions = CancelActions()
    harness.actions = actions
    cancelled = harness.reconcile()
    assert cancelled.overall_status is CIOverallStatus.CANCELLED
    assert cancelled.next_check_at == harness.clock() + timedelta(seconds=1)
    assert cancelled.unknown_since is None and cancelled.diagnosis is None
    assert harness.state() == "CI_RUNNING" and actions.rerun_calls == []

    actions.failure = CIProviderFailure.TRANSIENT
    assert harness.reconcile() == cancelled
    assert len(actions.calls) == 1 and actions.rerun_calls == []
    harness.clock.advance(1)
    queued = harness.reconcile()
    assert queued.overall_status is CIOverallStatus.QUEUED
    assert queued.id != cancelled.id and queued.attempt_number == 2
    assert queued.retry_count == 1 and queued.external_workflow_run_ids == (
        "actual-run",
    )
    assert actions.rerun_calls == [("owner/one", "actual-run")]
    assert len(actions.calls) == 1 and harness.state() == "CI_RUNNING"

    actions.failure = None
    actions.observation = CIObservation(
        (
            CICheck(
                "validate",
                "fresh-job",
                CICheckStatus.COMPLETED,
                CICheckConclusion.PASSED,
            ),
        ),
        ("actual-run",),
    )
    harness.clock.advance(1)
    passed = harness.reconcile()
    assert passed.id == queued.id and passed.attempt_number == 2
    assert passed.head_sha == HEAD and passed.overall_status is CIOverallStatus.PASSED
    assert harness.state() == "ARCHITECT_REVIEW"
    assert SQLiteCIRepository(harness.connection).get(cancelled.id) == cancelled
    assert actions.rerun_calls == [("owner/one", "actual-run")]


def test_repeated_real_cancellations_exhaust_retry_budget_without_codex_rework(
    harness: Harness,
) -> None:
    actions = CancelActions()
    harness.actions = actions
    cancelled = harness.reconcile()
    original_id = cancelled.id
    for attempt in (1, 2):
        assert cancelled.next_check_at is not None
        harness.clock.advance(
            (cancelled.next_check_at - harness.clock()).total_seconds()
        )
        queued = harness.reconcile()
        assert queued.overall_status is CIOverallStatus.QUEUED
        assert queued.retry_count == attempt
        assert queued.attempt_number == attempt + 1
        harness.clock.advance(1)
        cancelled = harness.reconcile()
        assert cancelled.overall_status is CIOverallStatus.CANCELLED
    assert cancelled.next_check_at is None and harness.state() == "BLOCKED"
    assert actions.rerun_calls == [("owner/one", "actual-run")] * 2
    assert cancelled.unknown_since is None
    assert SQLiteCIRepository(harness.connection).get(original_id).overall_status is (
        CIOverallStatus.CANCELLED
    )
    assert (
        harness.connection.execute(
            """SELECT count(*) FROM state_transitions WHERE milestone_id=?
        AND new_state IN ('CI_REWORK','ARCHITECT_REVIEW')""",
            (str(harness.milestone),),
        ).fetchone()[0]
        == 0
    )
    assert CIJobCoordinator(harness.connection).enqueue_due(harness.clock()) == 0


def test_new_head_gets_fresh_grace_without_reusing_previous_sha(
    harness: Harness,
) -> None:
    old = harness.reconcile()
    harness.clock.advance(30)
    harness.prs.head = NEW_HEAD
    fresh = harness.reconcile()
    assert fresh.id != old.id and fresh.head_sha == NEW_HEAD
    assert fresh.unknown_since == harness.clock()
    assert fresh.unknown_deadline_at == harness.clock() + timedelta(seconds=10)
    assert fresh.unknown_observation_count == 1
    assert harness.state() == "CI_RUNNING"
    assert SQLiteCIRepository(harness.connection).get(old.id) == old
    assert harness.actions.calls[-1] == ("owner/one", 12, NEW_HEAD)


def test_head_change_during_expiring_observation_prevents_stale_block(
    harness: Harness,
) -> None:
    first = harness.reconcile()
    harness.clock.advance(10)
    harness.prs.change_after_read = NEW_HEAD
    stale = harness.reconcile()
    assert stale.id == first.id and stale.head_sha == HEAD
    assert harness.state() == "CI_RUNNING"
    assert (
        harness.connection.execute(
            "SELECT head_sha FROM pull_requests WHERE id=?", (harness.pr_id,)
        ).fetchone()[0]
        == NEW_HEAD
    )
    assert (
        harness.connection.execute(
            "SELECT count(*) FROM state_transitions WHERE milestone_id=?",
            (str(harness.milestone),),
        ).fetchone()[0]
        == 0
    )
    harness.clock.advance(1)
    fresh = harness.reconcile()
    assert fresh.head_sha == NEW_HEAD and fresh.unknown_observation_count == 1


def test_head_change_during_provider_retry_exhaustion_prevents_stale_block(
    harness: Harness,
) -> None:
    harness.actions.failure = CIProviderFailure.TRANSIENT
    harness.reconcile()
    harness.clock.advance(1)
    assert harness.reconcile().retry_count == 2
    harness.clock.advance(2)
    harness.prs.change_after_read = NEW_HEAD
    stale = harness.reconcile()
    assert stale.head_sha == HEAD and stale.retry_count == 3
    assert stale.next_check_at is None
    assert stale.diagnosis is CIDiagnosis.PROVIDER_UNAVAILABLE
    assert harness.state() == "CI_RUNNING"
    harness.actions.failure = None
    harness.clock.advance(1)
    fresh = harness.reconcile()
    assert fresh.head_sha == NEW_HEAD and fresh.id != stale.id
    assert fresh.unknown_since == harness.clock()
    assert fresh.unknown_observation_count == 1 and fresh.retry_count == 0
    assert harness.state() == "CI_RUNNING"


@pytest.mark.parametrize("passing_on_resume", [False, True])
def test_pause_suppresses_work_and_resume_observes_provider_before_expiry(
    harness: Harness, passing_on_resume: bool
) -> None:
    first = harness.reconcile()
    harness.set_project_state(ProjectState.PAUSED)
    harness.clock.advance(100)
    pr_calls, action_calls = harness.prs.calls, len(harness.actions.calls)
    assert CIJobCoordinator(harness.connection).enqueue_due(harness.clock()) == 0
    with pytest.raises(CIMonitorError):
        harness.reconcile()
    assert harness.prs.calls == pr_calls
    assert len(harness.actions.calls) == action_calls
    assert SQLiteCIRepository(harness.connection).get(first.id) == first
    assert harness.state() == "CI_RUNNING"
    harness.set_project_state(ProjectState.BUILDING)
    if passing_on_resume:
        harness.actions.observation = _check_observation(
            CICheckStatus.COMPLETED, CICheckConclusion.PASSED
        )
    resumed = harness.reconcile()
    assert len(harness.actions.calls) == action_calls + 1
    if passing_on_resume:
        assert resumed.overall_status is CIOverallStatus.PASSED
        assert harness.state() == "ARCHITECT_REVIEW"
    else:
        assert resumed.unknown_since == first.unknown_since
        _assert_infrastructure_block(harness, resumed)


@pytest.mark.parametrize("stage", ["pull_request", "actions"])
def test_pause_during_provider_observation_prevents_evidence_and_transition(
    harness: Harness, stage: str
) -> None:
    first = harness.reconcile()
    harness.clock.advance(10)

    def pause() -> None:
        harness.set_project_state(ProjectState.PAUSED)

    if stage == "pull_request":
        harness.prs.on_get = pause
    else:
        harness.actions.on_observe = pause
    action_calls = len(harness.actions.calls)
    with pytest.raises(CIMonitorError):
        harness.reconcile()
    assert harness.state() == "CI_RUNNING"
    assert SQLiteCIRepository(harness.connection).get(first.id) == first
    assert CIJobCoordinator(harness.connection).enqueue_due(harness.clock()) == 0
    if stage == "pull_request":
        assert len(harness.actions.calls) == action_calls


@pytest.mark.parametrize("identity", ["project", "milestone"])
def test_live_pull_request_ownership_mismatch_stops_before_actions(
    harness: Harness, identity: str
) -> None:
    if identity == "project":
        harness.prs.observed_project = ProjectId.generate()
    else:
        harness.prs.observed_milestone = MilestoneId.generate()
    with pytest.raises(CIMonitorError):
        harness.reconcile()
    assert harness.actions.calls == []
    assert harness.connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 0
    assert harness.state() == "CI_RUNNING"


@pytest.mark.parametrize("stage", ["before_observation", "during_observation"])
def test_active_pull_request_substitution_cannot_block_or_rework(
    harness: Harness, stage: str
) -> None:
    first = harness.reconcile()
    harness.clock.advance(10)
    _, _, other_pr = _seed(harness.connection, harness.clock, "two")

    def substitute_active_pr() -> None:
        harness.connection.execute(
            "UPDATE milestones SET active_pull_request_id=? WHERE id=?",
            (other_pr, str(harness.milestone)),
        )

    if stage == "before_observation":
        substitute_active_pr()
    else:
        harness.actions.on_observe = substitute_active_pr
    action_calls = len(harness.actions.calls)
    with pytest.raises(CIMonitorError):
        harness.reconcile()
    assert harness.state() == "CI_RUNNING"
    assert SQLiteCIRepository(harness.connection).get(first.id) == first
    if stage == "before_observation":
        assert len(harness.actions.calls) == action_calls


def test_restart_restores_same_grace_and_block_is_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart.db"
    clock, prs, actions = FrozenClock(), PullRequests(), Actions()
    with closing(open_database(database)) as connection:
        apply_migrations(connection)
        project, milestone, pr_id = _seed(connection, clock)
        harness = Harness(connection, project, milestone, pr_id, clock, prs, actions)
        first = harness.reconcile()
    clock.advance(4)
    with closing(open_database(database)) as connection:
        harness = Harness(connection, project, milestone, pr_id, clock, prs, actions)
        restored = harness.reconcile()
        replay = harness.reconcile()
        assert first.id == restored.id == replay.id
        assert restored.unknown_since == first.unknown_since
        assert restored.unknown_deadline_at == first.unknown_deadline_at
        assert (
            restored.unknown_observation_count == replay.unknown_observation_count == 2
        )
        clock.advance(6)
        blocked = harness.reconcile()
        _assert_infrastructure_block(harness, blocked)
    clock.advance(100)
    with closing(open_database(database)) as connection:
        harness = Harness(connection, project, milestone, pr_id, clock, prs, actions)
        calls = len(actions.calls)
        assert SQLiteCIRepository(connection).get(blocked.id) == blocked
        with pytest.raises(CIMonitorError):
            harness.reconcile()
        assert len(actions.calls) == calls
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 1
        _assert_infrastructure_block(harness, blocked)
        assert CIJobCoordinator(connection).enqueue_due(clock()) == 0


@pytest.mark.parametrize(
    ("status", "conclusion", "classification", "target"),
    [
        (CIOverallStatus.PASSED, CICheckConclusion.PASSED, None, "ARCHITECT_REVIEW"),
        (
            CIOverallStatus.FAILED,
            CICheckConclusion.FAILED,
            CIFailureClassification.TEST,
            "CI_REWORK",
        ),
    ],
)
def test_restart_recovers_durable_terminal_evidence_before_transition(
    tmp_path: Path,
    status: CIOverallStatus,
    conclusion: CICheckConclusion,
    classification: CIFailureClassification | None,
    target: str,
) -> None:
    database = tmp_path / "terminal-restart.db"
    clock, prs, actions = FrozenClock(), PullRequests(), Actions()
    with closing(open_database(database)) as connection:
        apply_migrations(connection)
        project, milestone, pr_id = _seed(connection, clock)
        with transaction(connection):
            terminal = SQLiteCIRepository(connection).reconcile(
                str(project),
                str(milestone),
                pr_id,
                HEAD,
                status,
                (CICheck("validate", "job-1", CICheckStatus.COMPLETED, conclusion),),
                ("run-1",),
                classification,
                clock(),
            )
    clock.advance(100)
    actions.failure = CIProviderFailure.TRANSIENT
    with closing(open_database(database)) as connection:
        harness = Harness(connection, project, milestone, pr_id, clock, prs, actions)
        restored = harness.reconcile()
        assert restored == terminal and harness.state() == target
        assert actions.calls == []
        assert prs.calls >= 2
        assert SQLiteCIRepository(connection).get(terminal.id) == terminal
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 1


@pytest.mark.parametrize("status", [CIOverallStatus.PASSED, CIOverallStatus.FAILED])
def test_durable_terminal_evidence_is_not_reused_for_a_new_head(
    harness: Harness, status: CIOverallStatus
) -> None:
    conclusion = (
        CICheckConclusion.PASSED
        if status is CIOverallStatus.PASSED
        else CICheckConclusion.FAILED
    )
    with transaction(harness.connection):
        terminal = SQLiteCIRepository(harness.connection).reconcile(
            str(harness.project),
            str(harness.milestone),
            harness.pr_id,
            HEAD,
            status,
            _check_observation(CICheckStatus.COMPLETED, conclusion).checks,
            ("run-1",),
            CIFailureClassification.TEST if status is CIOverallStatus.FAILED else None,
            harness.clock(),
        )
    harness.clock.advance(100)
    harness.prs.head = NEW_HEAD
    current = harness.reconcile()
    assert current.head_sha == NEW_HEAD and current.id != terminal.id
    assert current.overall_status is CIOverallStatus.UNKNOWN
    assert current.unknown_since == harness.clock()
    assert current.unknown_observation_count == 1
    assert harness.actions.calls == [("owner/one", 12, NEW_HEAD)]
    assert harness.state() == "CI_RUNNING"
    assert SQLiteCIRepository(harness.connection).get(terminal.id) == terminal


def test_blocked_and_paused_projects_do_not_stop_other_project_ci_scheduling(
    harness: Harness,
) -> None:
    first = harness.reconcile()
    harness.clock.advance(10)
    _assert_infrastructure_block(harness, harness.reconcile())
    other_project, other_milestone, other_pr = _seed(
        harness.connection, harness.clock, "two"
    )
    other = Harness(
        harness.connection,
        other_project,
        other_milestone,
        other_pr,
        harness.clock,
        PullRequests(),
        Actions(),
    )
    waiting = other.reconcile()
    other.set_project_state(ProjectState.PAUSED)
    harness.clock.advance(1)
    coordinator = CIJobCoordinator(harness.connection)
    assert coordinator.enqueue_due(harness.clock()) == 0
    other.set_project_state(ProjectState.BUILDING)
    assert coordinator.enqueue_due(harness.clock()) == 1
    assert coordinator.enqueue_due(harness.clock()) == 0
    jobs = harness.connection.execute("SELECT * FROM jobs").fetchall()
    assert len(jobs) == 1 and jobs[0]["project_id"] == str(other_project)
    assert jobs[0]["milestone_id"] == str(other_milestone)
    assert jobs[0]["worker_class"] == "CI"
    assert SQLiteCIRepository(harness.connection).get(waiting.id) == waiting
    assert first.project_id == str(harness.project)
