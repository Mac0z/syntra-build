"""Single-process M30 operational runtime with thread-owned SQLite connections."""

from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from syntra_build.adapters.github.actions import GitHubActionsAdapter
from syntra_build.adapters.github.pull_requests import GitHubPullRequestAdapter
from syntra_build.adapters.telegram import (
    TelegramClient,
    TelegramDesignApprovalHandler,
    TelegramHumanInterventionHandler,
)
from syntra_build.application.ci_handoff import PullRequestCIHandoff
from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.application.dispatch_guard import DiskDispatchGuard
from syntra_build.application.gatekeeper import Gatekeeper
from syntra_build.application.human_intervention import HumanInterventionService
from syntra_build.application.operational_health import (
    LocalResourceSampler,
    OperationalHealth,
)
from syntra_build.application.production import (
    ProductionDueWorkCoordinator,
    build_production_executors,
)
from syntra_build.application.pull_requests import PullRequestLifecycleService
from syntra_build.application.recovery import RecoveryCoordinator, RecoveryServices
from syntra_build.application.scheduler import (
    Scheduler,
    SchedulerLoop,
    WorkerCapacity,
)
from syntra_build.application.security import SecurityPolicy
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain.jobs import WorkerClass
from syntra_build.infrastructure.backup import BackupReason, SQLiteBackupService
from syntra_build.infrastructure.config.host import (
    DEFAULT_HOST_CONFIG_PATH,
    load_host_config,
)
from syntra_build.infrastructure.config.models import ApplicationConfig
from syntra_build.infrastructure.health_http import HealthHTTPServer
from syntra_build.infrastructure.logging import configure_logging
from syntra_build.infrastructure.metrics import MetricsService, PrometheusRecorder
from syntra_build.infrastructure.persistence import bootstrap_database
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.cursors import (
    SQLiteProviderCursorRepository,
)
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.m22_smoke import trusted_git_from_host_config
from syntra_build.smoke import build_host_router, run_telegram_once

_LOGGER = logging.getLogger(__name__)


class ServiceRuntime:
    """Own all mutation on one control thread; HTTP and backup open their own DBs."""

    def __init__(
        self,
        config_path: Path = DEFAULT_HOST_CONFIG_PATH,
        *,
        config_loader: Callable[[Path], ApplicationConfig] = load_host_config,
        telegram_factory: Callable[
            [ApplicationConfig, PrometheusRecorder, SecurityPolicy], TelegramClient
        ]
        | None = None,
        loop_interval: float = 1.0,
        backup_interval: float = 3600.0,
        shutdown_grace: float = 30.0,
        configure_runtime_logging: bool = True,
        executor_factory: Callable[[ApplicationConfig], Mapping[WorkerClass, object]]
        | None = None,
    ) -> None:
        self.config = config_loader(config_path)
        if configure_runtime_logging:
            configure_logging(self.config)
        # Bootstrap is deliberately complete before any runtime thread can dispatch.
        bootstrap_database(self.config).close()
        self.stop_event = Event()
        self.ready_event = Event()
        self.failed_event = Event()
        self.capacity = WorkerCapacity(self.config.scheduler.worker_class_limits())
        self.recorder = PrometheusRecorder()
        self.health = OperationalHealth(
            LocalResourceSampler(self.config.filesystem.data_root),
            self.config.security,
            self._database_available,
        )
        self.backups = SQLiteBackupService(
            self.config.database.sqlite_path, self.config.filesystem.backup_root
        )
        self.http: HealthHTTPServer | None = None
        self.scheduler: Scheduler | None = None
        self._control_thread: Thread | None = None
        self._backup_thread: Thread | None = None
        self._telegram_factory = telegram_factory or (
            lambda config, metrics, security: TelegramClient(
                config, metrics=metrics, security_events=security
            )
        )
        self._loop_interval = loop_interval
        self._backup_interval = backup_interval
        self._shutdown_grace = shutdown_grace
        self._executor_factory = executor_factory

    def _production_executors(self) -> Mapping[WorkerClass, object]:
        """Use an explicit test seam or the reviewed production composition."""
        if self._executor_factory is not None:
            return self._executor_factory(self.config)
        return build_production_executors(self.config, metrics=self.recorder)

    def _database_available(self) -> bool:
        try:
            with sqlite3.connect(
                f"file:{self.config.database.sqlite_path.as_posix()}?mode=ro", uri=True
            ) as connection:
                row = connection.execute("SELECT 1").fetchone()
                return row is not None and int(row[0]) == 1
        except sqlite3.Error:
            return False

    def start(self) -> None:
        if self.config.metrics.enabled:
            metrics = MetricsService(
                lambda: open_database(self.config.database.sqlite_path),
                self.health,
                self.capacity,
                self.recorder,
            )
            self.http = HealthHTTPServer(
                self.config.metrics.bind_host,
                self.config.metrics.port,
                self.health,
                metrics,
            )
            self.http.start()
        self._control_thread = Thread(target=self._control_loop, name="syntra-control")
        self._control_thread.start()
        if self.config.backups.enabled:
            self._backup_thread = Thread(target=self._backup_loop, name="syntra-backup")
            self._backup_thread.start()
        if not self.ready_event.wait(timeout=10):
            raise RuntimeError("service control plane did not become ready")
        if self.failed_event.is_set():
            raise RuntimeError("service startup recovery failed")

    def _control_loop(self) -> None:
        connection = open_database(self.config.database.sqlite_path)
        try:
            due_work = ProductionDueWorkCoordinator(connection)
            scheduler = Scheduler(
                SQLiteJobRepository(connection, lambda: str(uuid4())),
                self.capacity,
                self._production_executors(),  # type: ignore[arg-type]
                due_work_enqueuer=due_work.enqueue_due,
                dispatch_guard=DiskDispatchGuard(
                    LocalResourceSampler(self.config.filesystem.data_root),
                    self.config.security,
                ),
                metrics=self.recorder,
            )
        except Exception:
            connection.close()
            self.health.recovery_failed()
            self.failed_event.set()
            self.ready_event.set()
            _LOGGER.exception(
                "Production composition failed",
                extra={"event": "production_composition_failed"},
            )
            return
        self.scheduler = scheduler
        scheduler.enter_drain()
        try:
            services, workspace = self._recovery_services(connection)

            def workspace_clean(subject: object) -> bool:
                from syntra_build.domain.recovery import RecoverySubject

                assert isinstance(subject, RecoverySubject)
                return bool(
                    workspace is not None
                    and subject.milestone_id is not None
                    and workspace.inspect(
                        subject.project_id, subject.milestone_id
                    ).clean
                )

            RecoveryCoordinator(
                connection,
                scheduler,
                services=services,
                workspace_clean=workspace_clean if workspace is not None else None,
                health_sink=self.health,
            ).recover()
            telegram = (
                self._telegram_factory(
                    self.config, self.recorder, SecurityPolicy(connection)
                )
                if self.config.telegram.enabled
                else None
            )
            router = (
                build_host_router(self.config, connection, health=self.health)
                if telegram
                else None
            )
            authorised = frozenset(
                str(i) for i in self.config.telegram.authorised_user_ids
            )
            design = (
                TelegramDesignApprovalHandler(connection, telegram, authorised)
                if telegram
                else None
            )
            human = (
                TelegramHumanInterventionHandler(
                    connection,
                    telegram,
                    authorised,
                    HumanInterventionService(
                        connection, authorised_responder_ids=authorised
                    ),
                )
                if telegram
                else None
            )
            cursors = SQLiteProviderCursorRepository(connection)
            # Recovery and all startup composition have succeeded. This is the one
            # transition that intentionally activates autonomous M32 scheduling.
            scheduler.exit_drain()
            _LOGGER.info(
                "Production scheduler activated",
                extra={"event": "scheduler_activated"},
            )
            self.ready_event.set()
            loop = SchedulerLoop(scheduler, self._loop_interval)
            while not self.stop_event.is_set():
                scheduler.run_once()
                if (
                    telegram is not None
                    and router is not None
                    and not self.stop_event.is_set()
                ):
                    try:
                        run_telegram_once(telegram, router, cursors, design, human)
                    except Exception:
                        _LOGGER.exception(
                            "Telegram poll failed",
                            extra={"event": "telegram_poll_failed"},
                        )
                        self.stop_event.wait(self._loop_interval)
                scheduler.wait_for_wake(self._loop_interval)
            loop.stop()
            scheduler.drain_until_idle(
                self._shutdown_grace,
                poll_interval_seconds=min(self._loop_interval, 0.1),
            )
        except Exception:
            self.health.recovery_failed()
            self.failed_event.set()
            self.ready_event.set()
            _LOGGER.exception(
                "Control plane failed", extra={"event": "control_plane_failed"}
            )
        finally:
            scheduler.close(wait=False)
            connection.close()

    def _recovery_services(
        self, connection: sqlite3.Connection
    ) -> tuple[RecoveryServices, WorkspaceService | None]:
        """Compose only released trusted observers; absent providers fail closed."""
        human = HumanInterventionService(
            connection,
            authorised_responder_ids=frozenset(
                str(item) for item in self.config.telegram.authorised_user_ids
            ),
        )
        if not self.config.github.enabled:
            return RecoveryServices(human=human), None
        git = trusted_git_from_host_config(
            self.config, self.config.filesystem.data_root
        )
        workspace = WorkspaceService(connection, git, self.config.filesystem.data_root)
        pull_requests = GitHubPullRequestAdapter(self.config)
        lifecycle = PullRequestLifecycleService(connection, workspace, pull_requests)
        return (
            RecoveryServices(
                pull_requests=PullRequestCIHandoff(connection, lifecycle),
                ci=CIMonitor(
                    connection,
                    pull_requests,
                    GitHubActionsAdapter(self.config, metrics=self.recorder),
                ),
                gatekeeper=Gatekeeper(connection, pull_requests),
                workspace=workspace,
                human=human,
            ),
            workspace,
        )

    def _backup_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.backups.automatic_due(datetime.now(UTC)):
                try:
                    item = self.backups.create(BackupReason.AUTOMATIC)
                    self.backups.retain(
                        self.config.backups.retention_days,
                        now=item.created_at,
                        keep=item.path,
                    )
                except Exception:
                    _LOGGER.exception(
                        "Automatic backup failed", extra={"event": "backup_failed"}
                    )
            self.stop_event.wait(self._backup_interval)

    def stop(self) -> None:
        self.health.draining()
        if self.scheduler is not None:
            self.scheduler.enter_drain()
        self.stop_event.set()
        if self.scheduler is not None:
            self.scheduler.wake()
        for thread in (self._control_thread, self._backup_thread):
            if thread is not None:
                thread.join(timeout=self._shutdown_grace)
        if self.http:
            self.http.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_HOST_CONFIG_PATH)
    args = parser.parse_args(argv)
    runtime = ServiceRuntime(args.config)
    stopped = Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stopped.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stopped.set())
    try:
        runtime.start()
        stopped.wait()
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
