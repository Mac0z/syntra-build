"""Bounded M29 host acceptance probe/temporary server."""

from __future__ import annotations

import argparse
import sqlite3
import time
import urllib.request
from pathlib import Path

from syntra_build.application.operational_health import (
    LocalResourceSampler,
    OperationalHealth,
)
from syntra_build.application.scheduler.capacity import WorkerCapacity
from syntra_build.infrastructure.config import (
    DEFAULT_ARCHITECT_API_KEY_PATH,
    DEFAULT_GITHUB_TOKEN_PATH,
    DEFAULT_HOST_CONFIG_PATH,
    DEFAULT_TELEGRAM_TOKEN_PATH,
    load_host_config,
)
from syntra_build.infrastructure.health_http import HealthHTTPServer
from syntra_build.infrastructure.metrics import MetricsService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve-seconds", type=float, default=0.0)
    parser.add_argument("--config", type=Path, default=DEFAULT_HOST_CONFIG_PATH)
    parser.add_argument(
        "--telegram-token", type=Path, default=DEFAULT_TELEGRAM_TOKEN_PATH
    )
    parser.add_argument("--github-token", type=Path, default=DEFAULT_GITHUB_TOKEN_PATH)
    parser.add_argument(
        "--architect-key", type=Path, default=DEFAULT_ARCHITECT_API_KEY_PATH
    )
    parser.add_argument(
        "--allow-disabled",
        action="store_true",
        help="run this bounded acceptance server even when metrics.enabled is false",
    )
    args = parser.parse_args()
    if args.serve_seconds < 0 or args.serve_seconds > 3600:
        parser.error("--serve-seconds must be between 0 and 3600")
    config = load_host_config(
        args.config,
        telegram_token_path=args.telegram_token,
        github_token_path=args.github_token,
        architect_api_key_path=args.architect_key,
    )
    if not config.metrics.enabled and not args.allow_disabled:
        parser.error(
            "metrics are disabled; use --allow-disabled for bounded acceptance"
        )
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
