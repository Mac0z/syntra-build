"""Single-process M30 operational runtime.

It composes released recovery, scheduling, health, metrics, and backup facilities;
it intentionally does not invent M32 workflow executors.
"""

from __future__ import annotations

import argparse
import logging
import signal
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from syntra_build.admin import _config
from syntra_build.application.operational_health import (
    LocalResourceSampler,
    OperationalHealth,
)
from syntra_build.application.recovery import RecoveryCoordinator
from syntra_build.application.scheduler import Scheduler, SchedulerLoop, WorkerCapacity
from syntra_build.infrastructure.backup import BackupReason, SQLiteBackupService
from syntra_build.infrastructure.config.host import DEFAULT_HOST_CONFIG_PATH
from syntra_build.infrastructure.health_http import HealthHTTPServer
from syntra_build.infrastructure.logging import configure_logging
from syntra_build.infrastructure.metrics import MetricsService, PrometheusRecorder
from syntra_build.infrastructure.persistence import bootstrap_database
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository

_LOGGER = logging.getLogger(__name__)


class ServiceRuntime:
    """Lifecycle object kept injectable for deterministic process/restart tests."""

    def __init__(self, config_path: Path = DEFAULT_HOST_CONFIG_PATH) -> None:
        self.config = _config(config_path)
        configure_logging(self.config)
        self.connection = bootstrap_database(self.config)
        self.stop_event = Event()
        self.capacity = WorkerCapacity(self.config.scheduler.worker_class_limits())
        self.recorder = PrometheusRecorder()
        self.health = OperationalHealth(
            LocalResourceSampler(self.config.filesystem.data_root),
            self.config.security,
            lambda: self.connection.execute("SELECT 1").fetchone() is not None,
        )
        self.scheduler = Scheduler(
            SQLiteJobRepository(self.connection, lambda: str(uuid4())),
            self.capacity,
            {},
            metrics=self.recorder,
        )
        self.scheduler.enter_drain()
        self.loop = SchedulerLoop(self.scheduler)
        self.backups = SQLiteBackupService(
            self.config.database.sqlite_path, self.config.filesystem.backup_root
        )
        self.http: HealthHTTPServer | None = None
        self.threads: list[Thread] = []

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
        RecoveryCoordinator(
            self.connection, self.scheduler, health_sink=self.health
        ).recover()
        scheduler_thread = Thread(target=self.loop.run, name="scheduler")
        backup_thread = Thread(target=self._backup_loop, name="backup")
        self.threads = [scheduler_thread, backup_thread]
        for thread in self.threads:
            thread.start()

    def _backup_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.config.backups.enabled and self.backups.automatic_due(
                datetime.now(UTC)
            ):
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
            self.stop_event.wait(3600)

    def stop(self) -> None:
        self.health.draining()
        self.scheduler.enter_drain()
        self.stop_event.set()
        self.loop.stop()
        for thread in self.threads:
            thread.join(timeout=30)
        self.scheduler.close(wait=False)
        if self.http:
            self.http.stop()
        self.connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_HOST_CONFIG_PATH)
    args = parser.parse_args(argv)
    runtime = ServiceRuntime(args.config)
    stopped = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        runtime.start()
        stopped.wait()
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
