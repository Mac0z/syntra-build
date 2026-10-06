from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from syntra_build.adapters.telegram.application import route_authorized_message
from syntra_build.adapters.telegram.models import TelegramInboundMessage
from syntra_build.application.commands.router import CommandRouter
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
    Scheduler,
    WorkerCapacity,
)
from syntra_build.domain import (
    EventProcessingStatus,
    Job,
    JobId,
    JobState,
    Project,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
    WorkerClass,
)
from syntra_build.infrastructure.config import SchedulerConfig, load_config
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    SQLiteProjectRepository,
    SQLiteWorkflowEventRepository,
    bootstrap_database,
)
from syntra_build.smoke import build_host_router

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


def _runtime(
    tmp_path: Path,
) -> tuple[
    sqlite3.Connection,
    SQLiteProjectRepository,
    SQLiteJobRepository,
    SQLiteWorkflowEventRepository,
    CommandRouter,
]:
    data = tmp_path / "data"
    for path in (data, tmp_path / "app", tmp_path / "etc", tmp_path / "log"):
        path.mkdir(parents=True, exist_ok=True)
    config = load_config(
        {
            "filesystem": {
                "application_root": str(tmp_path / "app"),
                "configuration_root": str(tmp_path / "etc"),
                "data_root": str(data),
                "log_root": str(tmp_path / "log"),
            },
            "database": {"sqlite_path": str(data / "syntra.db")},
        },
        environ={},
    )
    connection = bootstrap_database(config)
    identifiers = (f"m32-control-{index}" for index in range(1_000))
    projects = SQLiteProjectRepository(connection, lambda: next(identifiers))
    jobs = SQLiteJobRepository(connection, lambda: next(identifiers))
    events = SQLiteWorkflowEventRepository(connection)
    return (
        connection,
        projects,
        jobs,
        events,
        build_host_router(config, connection),
    )


def _project(
    projects: SQLiteProjectRepository,
    name: str,
    state: ProjectState,
    *,
    canonical_name: str | None,
    resume_state: ProjectState | None = None,
) -> Project:
    project = Project(
        ProjectId.generate(),
        name,
        state,
        NOW,
        NOW,
        resume_state=resume_state,
        canonical_name=canonical_name,
    )
    projects.add(project)
    return project


def _route(
    router: CommandRouter,
    text: str,
    update_id: int,
    when: datetime,
) -> str:
    return route_authorized_message(
        TelegramInboundMessage(
            update_id,
            update_id + 1_000,
            200,
            300,
            text,
            when,
        ),
        router,
    ).text


def _job(jobs: SQLiteJobRepository, project_id: ProjectId) -> JobId:
    job_id = JobId.generate()
    jobs.add(
        Job(
            job_id,
            project_id,
            "M32_CONTROL_TEST",
            JobState.QUEUED,
            0,
            NOW,
            NOW,
            worker_class=WorkerClass.CODEX,
            correlation_id=f"control-{job_id}",
        )
    )
    return job_id


def test_production_router_pauses_resumes_and_cancels_with_prior_audit(
    tmp_path: Path,
) -> None:
    connection, projects, jobs, events, router = _runtime(tmp_path)
    connection.execute(
        """CREATE TRIGGER require_command_audit BEFORE INSERT ON state_transitions
        WHEN NEW.entity_type='PROJECT' AND NEW.actor_type='HUMAN'
        BEGIN
          SELECT RAISE(ABORT, 'missing command audit')
          WHERE NEW.trigger_event_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM workflow_events
            WHERE id=NEW.trigger_event_id
              AND event_type='STATE_CHANGE_COMMAND_REQUESTED'
          );
        END"""
    )
    paused = _project(
        projects,
        "Pause Target",
        ProjectState.BUILDING,
        canonical_name="pause-target",
    )

    assert _route(router, "pause Pause Target", 1, NOW + timedelta(seconds=1)) == (
        "Project Pause Target paused. Resume target: BUILDING."
    )
    persisted = projects.get(paused.id)
    assert persisted.state is ProjectState.PAUSED
    assert persisted.resume_state is ProjectState.BUILDING
    pause_audit = events.for_project(paused.id)[0]
    pause_history = projects.transitions(paused.id)[0]
    assert pause_audit.processing_status is EventProcessingStatus.PROCESSED
    assert pause_audit.event.payload is not None
    assert pause_audit.event.payload["command"] == "PAUSE_PROJECT"
    assert pause_history.trigger_event_id == str(pause_audit.event.id)
    assert pause_history.actor_id == "300"

    assert _route(router, "resume Pause Target", 2, NOW + timedelta(seconds=2)) == (
        "Project Pause Target resumed to BUILDING."
    )
    resumed = projects.get(paused.id)
    assert resumed.state is ProjectState.BUILDING
    assert resumed.resume_state is None
    assert [item.new_state for item in projects.transitions(paused.id)] == [
        ProjectState.PAUSED,
        ProjectState.BUILDING,
    ]

    cancelled = _project(
        projects,
        "Cancel Target",
        ProjectState.READY,
        canonical_name="cancel-target",
    )
    projects.apply_transition(
        ProjectTransitionRequest(
            cancelled.id,
            ProjectState.READY,
            ProjectState.BUILDING,
            "Existing project history",
            "SYSTEM",
            "scheduler",
            "existing-history",
            NOW + timedelta(seconds=1),
        )
    )
    queued_job = _job(jobs, cancelled.id)
    existing_history = projects.transitions(cancelled.id)[0]

    assert _route(router, "cancel Cancel Target", 3, NOW + timedelta(seconds=3)) == (
        "Project Cancel Target cancelled."
    )
    assert projects.get(cancelled.id).state is ProjectState.CANCELLED
    history = projects.transitions(cancelled.id)
    assert history[0] == existing_history
    assert history[-1].new_state is ProjectState.CANCELLED
    assert jobs.get(queued_job, cancelled.id).state is JobState.QUEUED
    connection.close()


def test_invalid_and_repeated_commands_are_truthful_and_do_not_mutate(
    tmp_path: Path,
) -> None:
    connection, projects, _, events, router = _runtime(tmp_path)
    already_paused = _project(
        projects,
        "Already Paused",
        ProjectState.PAUSED,
        canonical_name="already-paused",
        resume_state=ProjectState.BUILDING,
    )
    building = _project(
        projects,
        "Still Building",
        ProjectState.BUILDING,
        canonical_name="still-building",
    )
    complete = _project(
        projects,
        "Already Complete",
        ProjectState.COMPLETE,
        canonical_name="already-complete",
    )
    missing_resume = _project(
        projects,
        "Missing Resume",
        ProjectState.PAUSED,
        canonical_name="missing-resume",
    )

    assert _route(router, "pause Already Paused", 10, NOW) == (
        "Project Already Paused is already paused."
    )
    assert _route(router, "resume Still Building", 11, NOW) == (
        "Project Still Building is not paused (state: BUILDING)."
    )
    assert _route(router, "cancel Already Complete", 12, NOW) == (
        "Project Already Complete cannot be cancelled from COMPLETE."
    )
    assert _route(router, "resume Missing Resume", 17, NOW) == (
        "Project Missing Resume cannot be resumed because no valid resume state "
        "is recorded."
    )
    for project in (already_paused, building, complete, missing_resume):
        assert projects.transitions(project.id) == ()
        assert len(events.for_project(project.id)) == 1

    repeat = _project(
        projects,
        "Repeat Target",
        ProjectState.BUILDING,
        canonical_name="repeat-target",
    )
    assert _route(router, "pause Repeat Target", 13, NOW) == (
        "Project Repeat Target paused. Resume target: BUILDING."
    )
    assert _route(router, "pause Repeat Target", 14, NOW) == (
        "Project Repeat Target is already paused."
    )
    assert len(projects.transitions(repeat.id)) == 1

    terminal = _project(
        projects,
        "Cancel Once",
        ProjectState.BUILDING,
        canonical_name="cancel-once",
    )
    assert _route(router, "cancel Cancel Once", 15, NOW) == (
        "Project Cancel Once cancelled."
    )
    assert _route(router, "cancel Cancel Once", 16, NOW) == (
        "Project Cancel Once is already cancelled."
    )
    assert len(projects.transitions(terminal.id)) == 1
    connection.close()


def test_legacy_project_without_canonical_name_is_mutable_by_exact_uuid(
    tmp_path: Path,
) -> None:
    connection, projects, _, _, router = _runtime(tmp_path)
    legacy = _project(
        projects,
        "Legacy Acceptance",
        ProjectState.BUILDING,
        canonical_name=None,
    )

    response = _route(router, f"cancel {legacy.id}", 20, NOW)

    assert response == "Project Legacy Acceptance cancelled."
    assert projects.get(legacy.id).state is ProjectState.CANCELLED
    connection.close()


@dataclass
class _ImmediateExecutor:
    calls: list[Job] = field(default_factory=list)

    def execute(self, job: Job) -> JobExecutionResult:
        self.calls.append(job)
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)


def test_pause_and_cancel_block_dispatch_without_affecting_other_project(
    tmp_path: Path,
) -> None:
    connection, projects, jobs, _, router = _runtime(tmp_path)
    pause_target = _project(
        projects,
        "Dispatch Pause",
        ProjectState.BUILDING,
        canonical_name="dispatch-pause",
    )
    cancel_target = _project(
        projects,
        "Dispatch Cancel",
        ProjectState.BUILDING,
        canonical_name="dispatch-cancel",
    )
    other = _project(
        projects,
        "Dispatch Other",
        ProjectState.BUILDING,
        canonical_name="dispatch-other",
    )
    paused_job = _job(jobs, pause_target.id)
    cancelled_job = _job(jobs, cancel_target.id)
    other_job = _job(jobs, other.id)

    assert "paused" in _route(router, "pause Dispatch Pause", 30, NOW)
    assert "cancelled" in _route(router, "cancel Dispatch Cancel", 31, NOW)

    executor = _ImmediateExecutor()
    capacity = WorkerCapacity(
        SchedulerConfig(codex_concurrency=1).worker_class_limits()
    )
    scheduler = Scheduler(
        jobs,
        capacity,
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW + timedelta(seconds=1),
    )
    result = scheduler.run_once()
    scheduler.wait_for_wake(2)
    scheduler.run_once()

    assert result.skipped_project_state == 2
    assert [job.id for job in executor.calls] == [other_job]
    assert jobs.get(paused_job, pause_target.id).state is JobState.QUEUED
    assert jobs.get(cancelled_job, cancel_target.id).state is JobState.QUEUED
    assert jobs.get(other_job, other.id).state is JobState.SUCCEEDED
    scheduler.close()
    connection.close()
