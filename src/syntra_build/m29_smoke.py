"""Bounded M29 host acceptance probe/temporary server."""

from __future__ import annotations

import argparse
import sqlite3
import time
import urllib.request

from syntra_build.application.operational_health import (
    LocalResourceSampler,
    OperationalHealth,
)
from syntra_build.application.scheduler.capacity import WorkerCapacity
from syntra_build.infrastructure.config.loader import load_config
from syntra_build.infrastructure.health_http import HealthHTTPServer
from syntra_build.infrastructure.metrics import MetricsService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve-seconds", type=float, default=0.0)
    args = parser.parse_args()
    if args.serve_seconds < 0 or args.serve_seconds > 3600:
        parser.error("--serve-seconds must be between 0 and 3600")
    config = load_config()
    sampler = LocalResourceSampler(config.filesystem.data_root)

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{config.database.sqlite_path}?mode=ro", uri=True)

    def database_ok() -> bool:
        with connect() as connection:
            result = connection.execute("SELECT 1").fetchone()
            return bool(result == (1,))

    health = OperationalHealth(sampler, config.security, database_ok)
    health.running()
    metrics = MetricsService(
        connect, health, WorkerCapacity(config.scheduler.worker_class_limits())
    )
    server = HealthHTTPServer(
        config.metrics.bind_host,
        config.metrics.port if args.serve_seconds else 0,
        health,
        metrics,
    )
    server.start()
    try:
        if args.serve_seconds:
            time.sleep(args.serve_seconds)
        else:
            host, port = server.address
            for path in ("health", "ready", "metrics"):
                with urllib.request.urlopen(
                    f"http://{host}:{port}/{path}", timeout=5
                ) as response:
                    if response.status != 200 or not response.read():
                        raise RuntimeError(f"{path} probe failed")
            print("M29 probe passed: /health /ready /metrics")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
