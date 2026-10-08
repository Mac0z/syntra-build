"""Telegram creation through production M32 routes and M17 notification recovery."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs
from urllib.request import Request
from uuid import uuid4

import pytest
from test_m32_service_composition import _config
from test_m32_specification_draft_executor import FakeSpecificationProvider

from syntra_build.adapters.github import GitHubHTTPResponse
from syntra_build.adapters.telegram import TelegramClient
from syntra_build.adapters.telegram.client import HTTPResponse
from syntra_build.application.architect import ArchitectProvider
from syntra_build.application.production import (
    ProductionDependencies,
    ProductionDueWorkCoordinator,
    build_production_executors,
)
from syntra_build.application.recovery import RecoveryCoordinator
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
    JobExecutor,
    Scheduler,
    WorkerCapacity,
)
from syntra_build.domain import (
    ArchitectDesignMode,
    ArchitectDesignRequest,
    ArchitectDesignResponse,
    DesignPackage,
    GateState,
    Job,
    JobId,
    JobState,
    JobTransitionRequest,
    ProjectId,
    ProjectState,
    WorkerClass,
)
from syntra_build.domain.failures import RetryBackoffPolicy
from syntra_build.infrastructure.config.models import SecretValue, TelegramConfig
from syntra_build.infrastructure.persistence import (
    SQLiteDesignPackageRepository,
    SQLiteHumanGateRepository,
    SQLiteJobRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    SQLiteProviderCursorRepository,
    SQLiteTelegramGateNotificationRepository,
    apply_migrations,
    open_database,
)
from syntra_build.smoke import build_host_router, run_telegram_once


class Architect(FakeSpecificationProvider):
    def __init__(self) -> None:
        super().__init__()
        self.design_requests: list[ArchitectDesignRequest] = []

    def design(self, request: ArchitectDesignRequest) -> ArchitectDesignResponse:
        self.design_requests.append(request)
        return ArchitectDesignResponse(
            request.interface_version,
            request.correlation_id,
            request.project_id,
            ArchitectDesignMode.PROPOSE_DESIGN,
            "Persisted design is ready to formalise.",
            (),
            (),
        )


class TelegramTransport:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.sent: list[dict[str, str]] = []
        self.notification_attempts = 0
        self.failure_code: int | None = None
        self.before_notification: Callable[[], None] = lambda: None

    @property
    def notifications(self) -> list[dict[str, str]]:
        return [item for item in self.sent if "reply_markup" in item]

    def __call__(self, request: Request, timeout: float) -> HTTPResponse:
        assert isinstance(request.data, bytes)
        parameters = {
            key: value[0] for key, value in parse_qs(request.data.decode()).items()
        }
        result: object
        if request.full_url.endswith("/getUpdates"):
            result, self.updates = self.updates, []
        else:
            assert request.full_url.endswith("/sendMessage")
            if "reply_markup" in parameters:
                self.before_notification()
                self.notification_attempts += 1
                if self.failure_code == 0:
                    raise TimeoutError("synthetic-provider-detail-not-for-logs")
                if self.failure_code is not None:
                    return HTTPResponse(
                        self.failure_code,
                        json.dumps(
                            {
                                "ok": False,
                                "error_code": self.failure_code,
                                "description": "synthetic-provider-detail-not-for-logs",
                            }
                        ).encode(),
                    )
            self.sent.append(parameters)
            result = {
                "message_id": 1000 + len(self.sent),
                "chat": {"id": int(parameters["chat_id"])},
                "message_thread_id": int(parameters["message_thread_id"]),
            }
        return HTTPResponse(200, json.dumps({"ok": True, "result": result}).encode())


class Workflow:
    def __init__(self, tmp_path: Path) -> None:
        base = _config(tmp_path)
        self.config = replace(
            base,
            telegram=TelegramConfig(enabled=True, authorised_user_ids=(30,)),
            secrets=replace(
                base.secrets, telegram_bot_token=SecretValue("synthetic-telegram-token")
            ),
        )
        self.transport = TelegramTransport()
        self.architect = Architect()
        self.shift = timedelta()
        self.update_id = 0
        self.db = open_database(self.config.database.sqlite_path)
        apply_migrations(self.db)
        self.compose()
        self.command("/create Notification Project | Build from durable context")
        self.project_id = (
            SQLiteProjectRepository(self.db, lambda: str(uuid4())).list_all()[0].id
        )

    def now(self) -> datetime:
        return datetime.now(UTC) + self.shift

    def compose(self) -> None:
        self.client = TelegramClient(self.config, transport=self.transport)
        self.executors = build_production_executors(
            self.config,
            dependencies=ProductionDependencies(
                architect_provider_factory=lambda: cast(
                    ArchitectProvider, self.architect
                ),
                telegram_client_factory=lambda: self.client,
            ),
        )
        self.router = build_host_router(
            self.config,
            self.db,
            github_transport=lambda _request, _timeout: GitHubHTTPResponse(404, b"{}"),
        )
        self.jobs = SQLiteJobRepository(self.db, lambda: str(uuid4()))
        self.due = ProductionDueWorkCoordinator(self.db, clock=self.now)
        self.scheduler = Scheduler(
            self.jobs,
            WorkerCapacity({worker: 1 for worker in WorkerClass}),
            cast(Mapping[WorkerClass, JobExecutor], self.executors),
            clock=self.now,
            due_work_enqueuer=self.due.enqueue_due,
            retry_policy=RetryBackoffPolicy((60,), jitter_factor=0),
        )

    def restart(self) -> None:
        self.scheduler.close()
        self.db.close()
        self.db = open_database(self.config.database.sqlite_path)
        self.compose()
        RecoveryCoordinator(self.db, self.scheduler, clock=self.now).recover()

    def command(self, text: str) -> str:
        self.update_id += 1
        self.transport.updates.append(
            {
                "update_id": self.update_id,
                "message": {
                    "message_id": 100 + self.update_id,
                    "from": {"id": 30},
                    "chat": {"id": -400},
                    "message_thread_id": 50,
                    "date": int(self.now().timestamp()) + (self.update_id > 1),
                    "text": text,
                },
            }
        )
        assert (
            run_telegram_once(
                self.client, self.router, SQLiteProviderCursorRepository(self.db)
            )
            == 1
        )
        return self.transport.sent[-1]["text"]

    def run_until(self, predicate: Callable[[], bool]) -> None:
        for _ in range(40):
            self.scheduler.run_once()
            if predicate():
                return
            if self.scheduler.active_execution_count:
                self.scheduler.wait_for_wake(2)
        jobs = self.db.execute("SELECT job_type,state,last_error_id FROM jobs")
        pytest.fail(f"workflow stalled: {[tuple(row) for row in jobs]}")

    def job(self, job_type: str) -> Job:
        row = self.db.execute(
            "SELECT id FROM jobs WHERE job_type=? ORDER BY rowid DESC LIMIT 1",
            (job_type,),
        ).fetchone()
        assert row is not None
        return self.jobs.get(JobId.from_string(row["id"]), self.project_id)

    def job_finished(self, job_type: str, state: JobState = JobState.SUCCEEDED) -> bool:
        row = self.db.execute(
            "SELECT state FROM jobs WHERE job_type=? ORDER BY rowid DESC LIMIT 1",
            (job_type,),
        ).fetchone()
        return row is not None and row["state"] == state.value

    def execute_without_harvest(self, job_type: str) -> JobExecutionResult:
        self.due.enqueue_due(self.now())
        job = self.job(job_type)
        self.jobs.claim_for_dispatch(
            JobTransitionRequest(
                job.id,
                job.project_id,
                JobState.QUEUED,
                JobState.DISPATCHED,
                "test dispatch",
                "SYSTEM",
                None,
                job.correlation_id,
                self.now(),
            ),
            tuple(ProjectState),
        )
        running = self.jobs.apply_transition(
            JobTransitionRequest(
                job.id,
                job.project_id,
                JobState.DISPATCHED,
                JobState.RUNNING,
                "test execution",
                "SYSTEM",
                None,
                job.correlation_id,
                self.now(),
            )
        )
        return cast(
            JobExecutionResult, self.executors[job.worker_class].execute(running)
        )

    def package(self) -> DesignPackage:
        package = SQLiteDesignPackageRepository(self.db).pending_for_project(
            self.project_id
        )
        assert package is not None
        return package

    def harvest_success(self, job_type: str) -> None:
        job = self.job(job_type)
        self.jobs.apply_transition(
            JobTransitionRequest(
                job.id,
                job.project_id,
                JobState.RUNNING,
                JobState.SUCCEEDED,
                "test harvest",
                "SYSTEM",
                None,
                job.correlation_id,
                self.now(),
            )
        )

    def generate_without_notification(self) -> None:
        for job_type in ("ARCHITECT_DESIGN", "SPECIFICATION_DRAFT"):
            assert (
                self.execute_without_harvest(job_type).disposition
                is JobExecutionDisposition.SUCCEEDED
            )
            self.harvest_success(job_type)

    def assert_artifacts(self, count: int = 1) -> None:
        for table in ("design_packages", "human_gates"):
            assert (
                self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
            )
        documents = SQLiteProjectDocumentRepository(self.db).for_project(
            self.project_id
        )
        assert len(documents) == 2 * count
        assert {
            kind: sum(item.document_type.value == kind for item in documents)
            for kind in ("SPEC", "AGENTS")
        } == {"SPEC": count, "AGENTS": count}
        assert sorted(item.revision for item in documents) == sorted(
            list(range(1, count + 1)) * 2
        )
        assert len(self.architect.requests) == count
        assert len(self.architect.design_requests) == 1


@pytest.fixture
def workflow(tmp_path: Path) -> Iterator[Workflow]:
    workflow = Workflow(tmp_path)
    try:
        yield workflow
    finally:
        workflow.scheduler.close()
        workflow.db.close()


def test_production_notification_and_fallback_revision_loop(
    workflow: Workflow, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    def committed_before_send() -> None:
        with open_database(workflow.config.database.sqlite_path) as observed:
            assert (
                observed.execute("SELECT state FROM projects").fetchone()[0]
                == "DESIGN_APPROVAL"
            )
            assert (
                observed.execute("SELECT count(*) FROM project_documents").fetchone()[0]
                == 2
            )
            assert (
                observed.execute("SELECT state FROM human_gates").fetchone()[0]
                == "PENDING"
            )

    workflow.transport.before_notification = committed_before_send
    workflow.run_until(lambda: workflow.job_finished("DESIGN_APPROVAL_NOTIFY"))
    workflow.assert_artifacts()
    package = workflow.package()
    assert workflow.db.execute("SELECT count(*) FROM milestones").fetchone()[0] == 0
    assert (
        workflow.db.execute("SELECT state FROM projects").fetchone()[0]
        == "DESIGN_APPROVAL"
    )
    gate = SQLiteHumanGateRepository(workflow.db, lambda: "unused").get(
        package.approval_gate_id
    )
    assert gate.state is GateState.NOTIFIED
    assert len(workflow.transport.notifications) == 1
    notice = workflow.transport.notifications[0]
    assert (notice["chat_id"], notice["message_thread_id"]) == ("-400", "50")
    buttons = [
        button
        for row in json.loads(notice["reply_markup"])["inline_keyboard"]
        for button in row
    ]
    assert [button["text"] for button in buttons] == [
        "View SPEC",
        "View AGENTS",
        "Approve",
        "Request changes",
    ]
    assert [button["callback_data"] for button in buttons] == [
        f"design:{action}:{gate.id}"
        for action in ("spec", "agents", "approve", "changes")
    ]
    binding = SQLiteTelegramGateNotificationRepository(workflow.db).get(gate.id)
    assert (binding.chat_id, binding.thread_id, binding.message_id) == (
        "-400",
        "50",
        "1002",
    )

    feedback = "Keep all required amounts in exact decimal arithmetic."
    assert "resolved as REJECTED" in workflow.command(
        f"gate {gate.id} REQUEST_CHANGES {feedback}"
    )
    assert (
        workflow.db.execute("SELECT state FROM projects").fetchone()[0] == "DESIGNING"
    )
    assert SQLiteDesignPackageRepository(workflow.db).feedback(workflow.project_id) == (
        feedback,
    )
    assert "already closed" in workflow.command(f"gate {gate.id} APPROVE")
    assert (
        workflow.db.execute("SELECT count(*) FROM human_gate_responses").fetchone()[0]
        == 1
    )

    workflow.transport.before_notification = lambda: None
    workflow.run_until(
        lambda: (
            len(workflow.transport.notifications) == 2
            and workflow.job_finished("DESIGN_APPROVAL_NOTIFY")
        )
    )
    workflow.assert_artifacts(2)
    assert workflow.architect.requests[1].prior_change_feedback == (feedback,)
    revised = workflow.package()
    assert revised.id != package.id
    assert revised.approval_gate_id != package.approval_gate_id
    assert "resolved as APPROVED" in workflow.command(
        f"gate {revised.approval_gate_id} APPROVE"
    )
    assert (
        workflow.db.execute("SELECT state FROM projects").fetchone()[0]
        == "PROVISIONING"
    )
    assert (
        workflow.db.execute(
            "SELECT count(*) FROM project_documents WHERE status='APPROVED'"
        ).fetchone()[0]
        == 2
    )
    events = {getattr(record, "event", None) for record in caplog.records}
    assert {
        "design_approval_notification_started",
        "gate_notification_sent",
        "design_approval_gate_notified",
    } <= events
    assert feedback not in caplog.text


@pytest.mark.parametrize("failure_code", [0, 429, 503])
def test_notification_failure_retries_only_notification_after_restart(
    workflow: Workflow, caplog: pytest.LogCaptureFixture, failure_code: int
) -> None:
    caplog.set_level(logging.INFO)
    workflow.transport.failure_code = failure_code
    workflow.run_until(
        lambda: workflow.job_finished("DESIGN_APPROVAL_NOTIFY", JobState.RETRY_WAIT)
    )
    workflow.assert_artifacts()
    package = workflow.package()
    gate_id = package.approval_gate_id
    before = (package.id, package.spec_document_id, package.agents_document_id, gate_id)
    assert (
        SQLiteHumanGateRepository(workflow.db, lambda: "unused").get(gate_id).state
        is GateState.PENDING
    )
    assert SQLiteTelegramGateNotificationRepository(workflow.db).find(gate_id) is None
    reply = workflow.command(f"gate {gate_id} REQUEST_CHANGES exact decimal arithmetic")
    assert "notification is still pending" in reply
    assert "already closed" not in reply
    assert (
        workflow.db.execute("SELECT count(*) FROM human_gate_responses").fetchone()[0]
        == 0
    )
    failed_job = workflow.job("DESIGN_APPROVAL_NOTIFY")
    workflow.restart()
    assert workflow.job("DESIGN_APPROVAL_NOTIFY").id == failed_job.id
    workflow.transport.failure_code = None
    workflow.shift += timedelta(minutes=2)
    workflow.run_until(lambda: workflow.job_finished("DESIGN_APPROVAL_NOTIFY"))
    workflow.assert_artifacts()
    package = workflow.package()
    assert before == (
        package.id,
        package.spec_document_id,
        package.agents_document_id,
        package.approval_gate_id,
    )
    assert workflow.transport.notification_attempts == 2
    assert len(workflow.transport.notifications) == 1
    assert workflow.job("DESIGN_APPROVAL_NOTIFY").attempt_number == 2
    assert (
        SQLiteHumanGateRepository(workflow.db, lambda: "unused").get(gate_id).state
        is GateState.NOTIFIED
    )
    failures = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "design_approval_notification_failed"
    ]
    assert len(failures) == 1
    assert getattr(failures[0], "project_id") == str(workflow.project_id)
    assert getattr(failures[0], "gate_id") == str(gate_id)
    assert "synthetic-provider-detail-not-for-logs" not in caplog.text
    assert "synthetic-telegram-token" not in caplog.text


def test_crash_after_binding_reuses_message_through_production_recovery(
    workflow: Workflow, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    # Persist the normal design job's completion; leave specification unharvested.
    assert (
        workflow.execute_without_harvest("ARCHITECT_DESIGN").disposition
        is JobExecutionDisposition.SUCCEEDED
    )
    workflow.harvest_success("ARCHITECT_DESIGN")
    assert (
        workflow.execute_without_harvest("SPECIFICATION_DRAFT").disposition
        is JobExecutionDisposition.SUCCEEDED
    )
    workflow.db.execute(
        """CREATE TRIGGER interrupt_notification BEFORE UPDATE OF state ON human_gates
           WHEN NEW.state='NOTIFIED'
           BEGIN SELECT RAISE(ABORT,'test crash boundary'); END"""
    )
    assert (
        workflow.execute_without_harvest("DESIGN_APPROVAL_NOTIFY").disposition
        is JobExecutionDisposition.FAILED
    )
    gate_id = workflow.package().approval_gate_id
    binding = SQLiteTelegramGateNotificationRepository(workflow.db).get(gate_id)
    assert (
        SQLiteHumanGateRepository(workflow.db, lambda: "unused").get(gate_id).state
        is GateState.PENDING
    )
    workflow.db.execute("DROP TRIGGER interrupt_notification")
    workflow.restart()
    assert (
        workflow.db.execute("SELECT state FROM projects").fetchone()[0]
        == "DESIGN_APPROVAL"
    )
    workflow.run_until(lambda: workflow.job_finished("DESIGN_APPROVAL_NOTIFY"))
    workflow.assert_artifacts()
    assert SQLiteTelegramGateNotificationRepository(workflow.db).get(gate_id) == binding
    assert (
        SQLiteHumanGateRepository(workflow.db, lambda: "unused").get(gate_id).state
        is GateState.NOTIFIED
    )
    assert workflow.transport.notification_attempts == 1
    assert len(workflow.transport.notifications) == 1
    assert (
        workflow.db.execute(
            "SELECT count(*) FROM jobs WHERE job_type='SPECIFICATION_DRAFT'"
        ).fetchone()[0]
        == 1
    )
    assert workflow.job("SPECIFICATION_DRAFT").state is JobState.ABANDONED
    assert "gate_notification_reused" in {
        getattr(record, "event", None) for record in caplog.records
    }


@pytest.mark.parametrize("failure_code", [403, 503])
def test_terminal_notification_failure_does_not_reset_retry_budget(
    workflow: Workflow, failure_code: int
) -> None:
    workflow.transport.failure_code = failure_code
    attempts = 1 if failure_code == 403 else 3
    for attempt in range(attempts):
        expected = JobState.FAILED if attempt == attempts - 1 else JobState.RETRY_WAIT
        workflow.run_until(
            lambda: workflow.job_finished("DESIGN_APPROVAL_NOTIFY", expected)
        )
        workflow.shift += timedelta(minutes=2)
    for _ in range(3):
        workflow.scheduler.run_once()
    workflow.assert_artifacts()
    assert workflow.transport.notification_attempts == attempts
    assert (
        workflow.db.execute(
            "SELECT count(*) FROM jobs WHERE job_type='DESIGN_APPROVAL_NOTIFY'"
        ).fetchone()[0]
        == 1
    )
    assert workflow.job("DESIGN_APPROVAL_NOTIFY").state is JobState.FAILED
    assert workflow.package() is not None


def test_restart_discovers_pre_fix_stranded_package_once(workflow: Workflow) -> None:
    workflow.generate_without_notification()
    package = workflow.package()
    assert (
        workflow.db.execute(
            "SELECT count(*) FROM jobs WHERE job_type='DESIGN_APPROVAL_NOTIFY'"
        ).fetchone()[0]
        == 0
    )
    workflow.restart()
    for _ in range(3):
        workflow.due.enqueue_due(workflow.now())
    assert (
        workflow.db.execute(
            "SELECT count(*) FROM jobs WHERE job_type='DESIGN_APPROVAL_NOTIFY'"
        ).fetchone()[0]
        == 1
    )
    workflow.run_until(lambda: workflow.job_finished("DESIGN_APPROVAL_NOTIFY"))
    workflow.assert_artifacts()
    assert workflow.package().id == package.id
    assert len(workflow.transport.notifications) == 1


def test_cross_project_locator_and_wrong_destination_fail_closed(
    workflow: Workflow,
) -> None:
    workflow.generate_without_notification()
    workflow.due.enqueue_due(workflow.now())
    job = workflow.job("DESIGN_APPROVAL_NOTIFY")
    executor = workflow.executors[WorkerClass.MESSAGING]
    result = cast(
        JobExecutionResult,
        executor.execute(replace(job, project_id=ProjectId.generate())),
    )
    assert result.disposition is JobExecutionDisposition.FAILED
    assert workflow.transport.notification_attempts == 0
    gate_id = workflow.package().approval_gate_id
    SQLiteTelegramGateNotificationRepository(workflow.db).add(
        gate_id, "999", "50", "9999", workflow.now()
    )
    result = cast(JobExecutionResult, executor.execute(job))
    assert result.disposition is JobExecutionDisposition.FAILED
    assert workflow.transport.notification_attempts == 0
    assert (
        SQLiteHumanGateRepository(workflow.db, lambda: "unused").get(gate_id).state
        is GateState.PENDING
    )
    workflow.assert_artifacts()


def test_pause_before_notification_suppresses_send_until_resume(
    workflow: Workflow,
) -> None:
    workflow.generate_without_notification()
    workflow.due.enqueue_due(workflow.now())
    workflow.command(f"pause {workflow.project_id}")
    workflow.run_until(
        lambda: workflow.job_finished("DESIGN_APPROVAL_NOTIFY", JobState.CANCELLED)
    )
    assert workflow.transport.notification_attempts == 0
    workflow.command(f"resume {workflow.project_id}")
    workflow.run_until(lambda: workflow.job_finished("DESIGN_APPROVAL_NOTIFY"))
    workflow.assert_artifacts()
    assert workflow.transport.notification_attempts == 1
