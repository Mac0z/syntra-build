from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from syntra_build.application.operational_health import OperationalHealth
from syntra_build.application.scheduler.capacity import WorkerCapacity
from syntra_build.domain.health import HealthState, ResourceSnapshot
from syntra_build.infrastructure.config.models import (
    ResourceThresholdConfig,
    SchedulerConfig,
)
from syntra_build.infrastructure.health_http import HealthHTTPServer
from syntra_build.infrastructure.metrics import MetricsService
from syntra_build.infrastructure.persistence import apply_migrations, open_database


class Sampler:
    def __init__(self, free: float = 50) -> None:
        self.free = free

    def sample(self) -> ResourceSnapshot:
        return ResourceSnapshot(100, int(100 - self.free), int(self.free), self.free)


def test_real_http_endpoints_and_observation_only_metrics(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    connection = open_database(database)
    assert apply_migrations(connection) == 25
    before = connection.total_changes

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
    finally:
        server.stop()
        after = connection.total_changes
        connection.close()
    assert after == before


def test_failure_metrics_are_bounded() -> None:
    from syntra_build.application.metrics import APIFailureClassification, APIProvider
    from syntra_build.infrastructure.metrics import PrometheusRecorder

    recorder = PrometheusRecorder()
    recorder.api_failure(APIProvider.TELEGRAM, APIFailureClassification.TRANSPORT)
    recorder.api_failure(APIProvider.GITHUB, APIFailureClassification.AUTHENTICATION)
    recorder.api_failure(APIProvider.ARCHITECT, APIFailureClassification.MALFORMED)
    text = "\n".join(recorder.samples())
    assert text.count("syntra_build_api_failures_total") == 3
    assert "synthetic secret/error/prompt" not in text
