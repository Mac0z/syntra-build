# ruff: noqa: E501
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from syntra_build.application.operational_health import OperationalHealth
from syntra_build.application.scheduler.capacity import WorkerCapacity
from syntra_build.domain.health import HealthState, ResourceSnapshot
from syntra_build.infrastructure.config.models import (
    ResourceThresholdConfig,
    SchedulerConfig,
)
from syntra_build.infrastructure.health_http import HealthHTTPServer
from syntra_build.infrastructure.metrics import MetricsService, PrometheusRecorder
from syntra_build.infrastructure.persistence import apply_migrations, open_database


class Sampler:
    def __init__(self, free: float = 50) -> None:
        self.free = free

    def sample(self) -> ResourceSnapshot:
        return ResourceSnapshot(100, int(100 - self.free), int(self.free), self.free)


def test_real_http_endpoints_and_observation_only_metrics(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    connection = open_database(database)
    assert apply_migrations(connection) == 26
    before = connection.total_changes
    observed_tables = (
        "projects",
        "milestones",
        "jobs",
        "job_attempts",
        "human_gates",
        "ci_runs",
        "architect_requests",
        "codex_runs",
        "merge_attempts",
        "state_transitions",
    )
    snapshots = {
        table: tuple(connection.execute(f"SELECT * FROM {table}"))
        for table in observed_tables
    }

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True)

    sampler = Sampler()
    health = OperationalHealth(sampler, ResourceThresholdConfig())
    metrics = MetricsService(
        connect,
        health,
        WorkerCapacity(SchedulerConfig().worker_class_limits()),
    )
    server = HealthHTTPServer("127.0.0.1", 0, health, metrics)
    server.start()
    host, port = server.address

    def get(path: str) -> tuple[int, str, bytes]:
        request = urllib.request.Request(f"http://{host}:{port}{path}")
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            return error.code, error.headers["Content-Type"], error.read()
        with response:
            return response.status, response.headers["Content-Type"], response.read()

    try:
        status, content_type, body = get("/ready")
        assert status == 503
        assert content_type == "application/json"
        assert json.loads(body) == {"ready": False}
        health.running()
        assert get("/health")[0] == 200
        status, content_type, body = get("/metrics")
        assert status == 200
        assert content_type.startswith("text/plain; version=0.0.4")
        text = body.decode()
        assert "syntra_build_ready 1" in text
        assert 'syntra_build_system_health{state="HEALTHY"} 1' in text
        assert "syntra_build_codex_run_duration_seconds_count 0" in text
        assert get("/missing")[0] == 404
        sampler.free = 4
        assert get("/health")[0] == 503
        assert get("/ready")[0] == 503
        assert json.loads(get("/health")[2])["state"] == HealthState.UNHEALTHY
        assert get("/metrics")[0] == 200
        for _ in range(2):
            assert get("/health")[0] == 503
            assert get("/ready")[0] == 503
            assert get("/metrics")[0] == 200
    finally:
        server.stop()
        after = connection.total_changes
        resulting = {
            table: tuple(connection.execute(f"SELECT * FROM {table}"))
            for table in observed_tables
        }
        connection.close()
    assert after == before
    assert resulting == snapshots


def test_failure_metrics_are_bounded() -> None:
    from syntra_build.application.metrics import APIFailureClassification, APIProvider
    from syntra_build.infrastructure.metrics import PrometheusRecorder

    recorder = PrometheusRecorder()
    recorder.api_failure(APIProvider.TELEGRAM, APIFailureClassification.TRANSPORT)
    recorder.api_failure(APIProvider.GITHUB, APIFailureClassification.AUTHENTICATION)
    recorder.api_failure(APIProvider.ARCHITECT, APIFailureClassification.MALFORMED)
    text = "\n".join(recorder.samples())
    assert (
        sum(
            line.startswith("syntra_build_api_failures_total{")
            for line in text.splitlines()
        )
        == 3
    )
    assert "synthetic secret/error/prompt" not in text


def test_metrics_service_accepts_shared_runtime_recorder(tmp_path: Path) -> None:
    database = tmp_path / "shared-recorder.sqlite3"
    with open_database(database) as connection:
        apply_migrations(connection)

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True)

    health = OperationalHealth(Sampler(), ResourceThresholdConfig())
    recorder = PrometheusRecorder()
    service = MetricsService(
        connect,
        health,
        WorkerCapacity(SchedulerConfig().worker_class_limits()),
        recorder=recorder,
    )
    assert service.recorder is recorder


def test_database_unavailable_is_bounded_and_not_ready(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with open_database(database) as connection:
        apply_migrations(connection)

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True)

    health = OperationalHealth(
        Sampler(),
        ResourceThresholdConfig(),
        database_check=lambda: (_ for _ in ()).throw(
            sqlite3.OperationalError("RAW_SQLITE_SECRET")
        ),
    )
    health.running()
    server = HealthHTTPServer(
        "127.0.0.1",
        0,
        health,
        MetricsService(
            connect,
            health,
            WorkerCapacity(SchedulerConfig().worker_class_limits()),
        ),
    )
    server.start()
    host, port = server.address
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://{host}:{port}/ready", timeout=2)
        assert caught.value.code == 503
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2)
        body = caught.value.read().decode()
        assert caught.value.code == 503
        assert '"state":"UNHEALTHY"' in body
        assert "DATABASE_UNAVAILABLE" in body
        assert "RAW_SQLITE_SECRET" not in body
    finally:
        server.stop()


def test_representative_aggregates_histograms_and_sensitive_exclusion(
    tmp_path: Path,
) -> None:
    database = tmp_path / "metrics.sqlite3"
    connection = open_database(database)
    apply_migrations(connection)
    connection.execute("PRAGMA foreign_keys=OFF")
    now = "2026-01-01T00:00:00+00:00"
    connection.execute(
        "INSERT INTO projects(id,name,state,created_at,updated_at,last_state_change_at) VALUES(?,?,?,?,?,?)",
        (
            "00000000-0000-0000-0000-000000000099",
            "SECRET_PROJECT_DO_NOT_EXPORT",
            "BUILDING",
            now,
            now,
            now,
        ),
    )
    connection.execute(
        "INSERT INTO projects(id,name,state,created_at,updated_at,last_state_change_at) VALUES(?,?,?,?,?,?)",
        ("project-two", "other", "PAUSED", now, now, now),
    )
    for job_id, worker, state in (
        ("job-secret", "CODEX", "RUNNING"),
        ("job-message", "MESSAGING", "QUEUED"),
    ):
        connection.execute(
            "INSERT INTO jobs(id,project_id,job_type,state,priority,correlation_id,attempt_number,max_attempts,worker_class,payload_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                "00000000-0000-0000-0000-000000000099",
                "TYPE",
                state,
                0,
                "human prompt do not export",
                0,
                1,
                worker,
                "{}",
                "{}",
                now,
                now,
            ),
        )
    connection.execute(
        "INSERT INTO codex_runs(id,project_id,milestone_id,job_id,worktree_id,correlation_id,interface_version,attempt_number,task_payload_json,task_content_hash,process_status,worker_identity,started_at,completed_at,timeout_seconds,stdout_reference,stderr_reference,error_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "codex",
            "00000000-0000-0000-0000-000000000099",
            "milestone",
            "job-secret",
            "worktree",
            "corr",
            "1.0",
            1,
            '{"prompt":"SECRET_PROMPT"}',
            "b" * 64,
            "FAILED",
            "worker",
            now,
            "2026-01-01T00:00:10+00:00",
            100.0,
            "secret-log",
            "secret-error",
            "RAW_ERROR_DO_NOT_EXPORT",
        ),
    )
    connection.execute(
        "INSERT INTO architect_requests(id,project_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,completed_at,status,failure_classification) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "architect",
            "00000000-0000-0000-0000-000000000099",
            "REVIEW",
            "openai",
            "model",
            "high",
            "1",
            '{"prompt":"SECRET_PROMPT"}',
            "corr",
            now,
            "2026-01-01T00:01:00+00:00",
            "FAILED",
            "RAW_ERROR_DO_NOT_EXPORT",
        ),
    )
    connection.execute(
        "INSERT INTO ci_runs(id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,overall_status,started_at,completed_at,last_checked_at,summary_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ci",
            "00000000-0000-0000-0000-000000000099",
            "milestone",
            "repository-name-do-not-export",
            "a" * 40,
            1,
            "PASSED",
            now,
            "2026-01-01T00:05:00+00:00",
            now,
            '{"secret":"credential-like-xyz"}',
        ),
    )
    connection.execute(
        "INSERT INTO state_transitions(id,entity_type,entity_id,project_id,previous_state,new_state,reason,actor_type,correlation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "transition",
            "PROJECT",
            "00000000-0000-0000-0000-000000000099",
            "00000000-0000-0000-0000-000000000099",
            "NEW",
            "BUILDING",
            "RAW_ERROR_DO_NOT_EXPORT",
            "SYSTEM",
            "corr",
            now,
        ),
    )
    connection.close()

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True)

    health = OperationalHealth(Sampler(), ResourceThresholdConfig())
    health.running()
    text = (
        MetricsService(
            connect, health, WorkerCapacity(SchedulerConfig().worker_class_limits())
        )
        .render()
        .decode()
    )
    for sample in (
        'syntra_build_projects{state="BUILDING"} 1',
        'syntra_build_projects{state="PAUSED"} 1',
        'syntra_build_jobs{worker_class="CODEX",state="RUNNING"} 1',
        'syntra_build_jobs{worker_class="MESSAGING",state="QUEUED"} 1',
        'syntra_build_codex_runs_total{status="FAILED"} 1',
        'syntra_build_architect_requests_total{request_type="REVIEW",status="FAILED"} 1',
        'syntra_build_ci_runs_total{status="PASSED"} 1',
        'syntra_build_state_transitions_total{entity_type="PROJECT",new_state="BUILDING"} 1',
        'syntra_build_codex_run_duration_seconds_bucket{le="15.0"} 1',
        "syntra_build_codex_run_duration_seconds_count 1",
        "syntra_build_codex_run_duration_seconds_sum 10",
        'syntra_build_architect_request_duration_seconds_bucket{le="60.0"} 1',
        "syntra_build_architect_request_duration_seconds_count 1",
        "syntra_build_architect_request_duration_seconds_sum 60",
        'syntra_build_ci_run_duration_seconds_bucket{le="300.0"} 1',
        'syntra_build_ci_run_duration_seconds_bucket{le="+Inf"} 1',
        "syntra_build_ci_run_duration_seconds_count 1",
        "syntra_build_ci_run_duration_seconds_sum 300",
    ):
        assert sample in text
    for histogram in ("codex_run", "architect_request", "ci_run"):
        assert f"# TYPE syntra_build_{histogram}_duration_seconds histogram" in text
    for sensitive in (
        "SECRET_PROJECT_DO_NOT_EXPORT",
        "00000000-0000-0000-0000-000000000099",
        "a" * 40,
        "repository-name-do-not-export",
        "RAW_ERROR_DO_NOT_EXPORT",
        "SECRET_PROMPT",
        "credential-like-xyz",
    ):
        assert sensitive not in text


def test_histogram_uses_one_sql_aggregate_row_not_history_rows() -> None:
    from collections.abc import Iterator
    from typing import cast

    class AggregateCursor:
        def fetchone(self) -> tuple[int | float, ...]:
            return (2, 16.0, 0, 1, 1, 2, 2, 2, 2, 2)

        def __iter__(self) -> Iterator[object]:
            raise AssertionError("collector must not iterate completed history rows")

    class AggregateConnection:
        calls = 0

        def execute(self, sql: str, parameters: object = ()) -> AggregateCursor:
            self.calls += 1
            assert "SUM(CASE WHEN" in sql
            assert "COUNT(*)" in sql
            assert parameters == tuple(BUCKETS)
            return AggregateCursor()

    from syntra_build.infrastructure.metrics import BUCKETS

    connection = AggregateConnection()
    lines: list[str] = []
    MetricsService._histogram(
        lines,
        cast(sqlite3.Connection, connection),
        "codex_runs",
        "syntra_build_codex_run_duration_seconds",
    )
    assert connection.calls == 1
    assert "syntra_build_codex_run_duration_seconds_count 2" in lines
    assert "syntra_build_codex_run_duration_seconds_sum 16" in lines
