from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from syntra_build.application.retries import ExhaustionNotice, escalate_exhausted_job
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.jobs import Job, JobState
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import Milestone, MilestoneState
from syntra_build.domain.projects import Project, ProjectState
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
P1 = ProjectId.from_string("00000000-0000-0000-0000-000000000001")
M1 = MilestoneId.from_string("00000000-0000-0000-0000-000000000011")


def transition(
    source: MilestoneState, target: MilestoneState
) -> MilestoneTransitionRequest:
    return MilestoneTransitionRequest(
        M1,
        P1,
        source,
        target,
        "logical rework",
        "SYSTEM",
        "test",
        "corr",
        NOW + timedelta(seconds=1),
    )


def repositories(path: Path) -> tuple[SQLiteMilestoneRepository, SQLiteJobRepository]:
    db = open_database(path)
    apply_migrations(db)
    SQLiteProjectRepository(db, lambda: "p-history").add(
        Project(P1, "one", ProjectState.BUILDING, NOW, NOW)
    )
    milestones = SQLiteMilestoneRepository(
        db, iter(f"m-{x}" for x in range(20)).__next__
    )
    jobs = SQLiteJobRepository(db, iter(f"j-{x}" for x in range(20)).__next__)
    return milestones, jobs


def test_rework_counter_and_transition_are_atomic_and_explicit(tmp_path: Path) -> None:
    milestones, _ = repositories(tmp_path / "counter.db")
    milestones.add(
        Milestone(M1, P1, 1, "M1", "one", MilestoneState.CI_RUNNING, NOW, NOW)
    )
    changed = milestones.apply_rework_transition(
        transition(MilestoneState.CI_RUNNING, MilestoneState.CI_REWORK),
        "ci",
        5,
        exhaustion_reason="CI rework exhausted",
    )
    assert changed.state is MilestoneState.CI_REWORK
    assert changed.ci_rework_count == 1
    assert changed.codex_cycle_count == 0
    assert changed.architect_rework_count == 0
    assert changed.human_test_rework_count == 0


@dataclass
class ObservingNotifier:
    milestones: SQLiteMilestoneRepository
    notices: list[ExhaustionNotice] = field(default_factory=list)

    def notify(self, notice: ExhaustionNotice) -> None:
        assert (
            self.milestones.get(notice.milestone_id, notice.project_id).state
            is MilestoneState.BLOCKED
        )
        self.notices.append(notice)


def test_attempt_exhaustion_blocks_before_notification(tmp_path: Path) -> None:
    milestones, jobs = repositories(tmp_path / "exhaustion.db")
    milestones.add(Milestone(M1, P1, 1, "M1", "one", MilestoneState.CODING, NOW, NOW))
    job = Job(
        JobId.generate(),
        P1,
        "CODEX",
        JobState.FAILED,
        4,
        NOW,
        NOW,
        milestone_id=M1,
        max_attempts=4,
        last_error_id="timeout",
        failure_classification=FailureClassification.TRANSIENT,
        retry_exhausted=True,
        exhaustion_reason="infrastructure retry limit exhausted",
    )
    jobs.add(job)
    notifier = ObservingNotifier(milestones)
    blocked = escalate_exhausted_job(
        job, milestones, notifier, NOW + timedelta(seconds=2)
    )
    assert blocked is not None and blocked.state is MilestoneState.BLOCKED
    assert len(notifier.notices) == 1
    assert "Human intervention is required" in notifier.notices[0].message()
