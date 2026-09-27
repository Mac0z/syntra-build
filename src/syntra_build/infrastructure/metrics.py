"""Prometheus exposition from bounded local and SQLite snapshots."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from threading import Lock

from syntra_build.application.metrics import (
    APIFailureClassification,
    APIProvider,
    MetricsRecorder,
)
from syntra_build.application.operational_health import OperationalHealth
from syntra_build.application.scheduler.capacity import WorkerCapacity
from syntra_build.domain.health import HealthState
from syntra_build.domain.jobs import JobState, WorkerClass
from syntra_build.domain.projects import ProjectState

BUCKETS = (1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0, 14400.0)


def _sample(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    label_text = ""
    if labels:
        label_text = (
            "{" + ",".join(f'{key}="{value}"' for key, value in labels.items()) + "}"
        )
    return f"{name}{label_text} {float(value):.15g}"


class PrometheusRecorder(MetricsRecorder):
    def __init__(self) -> None:
        self.api_failures: Counter[tuple[str, str]] = Counter()
        self.guard_denials: Counter[str] = Counter()
        self._lock = Lock()

    def api_failure(
        self, provider: APIProvider, classification: APIFailureClassification
    ) -> None:
        with self._lock:
            self.api_failures[(provider.value, classification.value)] += 1

    def resource_guard_denied(self, reason: str) -> None:
        if reason not in {"DISK_WARNING", "DISK_CODEX_STOP", "DISK_CRITICAL"}:
            raise ValueError("unbounded resource guard reason")
        with self._lock:
            self.guard_denials[reason] += 1

    def samples(self) -> list[str]:
        with self._lock:
            lines = ["# TYPE syntra_build_api_failures_total counter"]
            lines += [
                _sample(
                    "syntra_build_api_failures_total",
                    count,
                    {"provider": p, "classification": c},
                )
                for (p, c), count in sorted(self.api_failures.items())
            ]
            lines.append("# TYPE syntra_build_resource_guard_denials_total counter")
            lines += [
                _sample(
                    "syntra_build_resource_guard_denials_total",
                    count,
                    {"reason": reason},
                )
                for reason, count in sorted(self.guard_denials.items())
            ]
            return lines


class MetricsService:
    """Open a fresh read-only connection and never mutate during a scrape."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        health: OperationalHealth,
        capacity: WorkerCapacity,
    ) -> None:
        self._connections, self._health, self._capacity = (
            connection_factory,
            health,
            capacity,
        )
        self.recorder = PrometheusRecorder()

    def render(self) -> bytes:
        projection = self._health.projection()
        lines = [
            "# HELP syntra_build_ready Operational readiness",
            "# TYPE syntra_build_ready gauge",
            _sample("syntra_build_ready", int(projection.ready)),
            "# TYPE syntra_build_system_health gauge",
        ]
        for health_state in HealthState:
            lines.append(
                _sample(
                    "syntra_build_system_health",
                    int(health_state is projection.state),
                    {"state": health_state.value},
                )
            )
        resource = projection.resources
        for suffix, value in (
            ("disk_total_bytes", resource.total_bytes),
            ("disk_used_bytes", resource.used_bytes),
            ("disk_free_bytes", resource.free_bytes),
            ("disk_free_percent", resource.free_percent),
            ("artifact_bytes", resource.artifact_bytes),
        ):
            lines.append(f"# TYPE syntra_build_{suffix} gauge")
            lines.append(_sample(f"syntra_build_{suffix}", value))
        lines.append("# TYPE syntra_build_worker_capacity gauge")
        for worker in WorkerClass:
            for kind, value in (
                ("limit", self._capacity.capacity(worker)),
                ("in_use", self._capacity.in_use(worker)),
                ("available", self._capacity.available(worker)),
            ):
                lines.append(
                    _sample(
                        "syntra_build_worker_capacity",
                        value,
                        {"worker_class": worker.value, "kind": kind},
                    )
                )
        with closing(self._connections()) as connection:
            lines.append("# TYPE syntra_build_projects gauge")
            counts = dict(
                connection.execute("SELECT state,count(*) FROM projects GROUP BY state")
            )
            for project_state in ProjectState:
                lines.append(
                    _sample(
                        "syntra_build_projects",
                        counts.get(project_state.value, 0),
                        {"state": project_state.value},
                    )
                )
            job_counts = {
                (r[0], r[1]): r[2]
                for r in connection.execute(
                    "SELECT worker_class,state,count(*) FROM jobs "
                    "GROUP BY worker_class,state"
                )
            }
            lines.append("# TYPE syntra_build_jobs gauge")
            for worker in WorkerClass:
                for job_state in JobState:
                    lines.append(
                        _sample(
                            "syntra_build_jobs",
                            job_counts.get((worker.value, job_state.value), 0),
                            {"worker_class": worker.value, "state": job_state.value},
                        )
                    )
            self._group(
                lines,
                connection,
                "codex_runs",
                "process_status",
                "syntra_build_codex_runs_total",
                ("status",),
                metric_type="counter",
            )
            self._group(
                lines,
                connection,
                "architect_requests",
                "request_type,status",
                "syntra_build_architect_requests_total",
                ("request_type", "status"),
                metric_type="counter",
            )
            self._group(
                lines,
                connection,
                "ci_runs",
                "overall_status",
                "syntra_build_ci_runs_total",
                ("status",),
                metric_type="counter",
            )
            self._group(
                lines,
                connection,
                "state_transitions",
                "entity_type,new_state",
                "syntra_build_state_transitions_total",
                ("entity_type", "new_state"),
                metric_type="counter",
            )
            self._histogram(
                lines,
                connection,
                "codex_runs",
                "syntra_build_codex_run_duration_seconds",
            )
            self._histogram(
                lines,
                connection,
                "architect_requests",
                "syntra_build_architect_request_duration_seconds",
            )
            self._histogram(
                lines, connection, "ci_runs", "syntra_build_ci_run_duration_seconds"
            )
        lines.extend(self.recorder.samples())
        return ("\n".join(lines) + "\n").encode()

    @staticmethod
    def _group(
        lines: list[str],
        connection: sqlite3.Connection,
        table: str,
        columns: str,
        name: str,
        labels: tuple[str, ...],
        *,
        metric_type: str,
    ) -> None:
        lines.append(f"# TYPE {name} {metric_type}")
        rows = connection.execute(
            f"SELECT {columns},count(*) FROM {table} GROUP BY {columns}"
        )
        for row in rows:
            lines.append(
                _sample(name, row[-1], dict(zip(labels, row[:-1], strict=True)))
            )

    @staticmethod
    def _histogram(
        lines: list[str], connection: sqlite3.Connection, table: str, name: str
    ) -> None:
        lines.append(f"# TYPE {name} histogram")
        durations = [
            (
                datetime.fromisoformat(done) - datetime.fromisoformat(start)
            ).total_seconds()
            for start, done in connection.execute(
                f"SELECT started_at,completed_at FROM {table} "
                "WHERE completed_at IS NOT NULL"
            )
        ]
        for bound in BUCKETS:
            lines.append(
                _sample(
                    f"{name}_bucket",
                    sum(value <= bound for value in durations),
                    {"le": str(bound)},
                )
            )
        lines.append(_sample(f"{name}_bucket", len(durations), {"le": "+Inf"}))
        lines.append(_sample(f"{name}_count", len(durations)))
        lines.append(_sample(f"{name}_sum", sum(durations)))
