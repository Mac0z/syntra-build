from __future__ import annotations

import json
import socket
from pathlib import Path
from urllib.request import urlopen

from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.config.models import ApplicationConfig
from syntra_build.service import ServiceRuntime


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(tmp_path: Path, *, metrics: bool = True) -> ApplicationConfig:
    data = tmp_path / "data"
    for path in (data, tmp_path / "app", tmp_path / "etc", tmp_path / "log"):
        path.mkdir(parents=True, exist_ok=True)
    return load_config(
        {
            "filesystem": {
                "application_root": str(tmp_path / "app"),
                "configuration_root": str(tmp_path / "etc"),
                "data_root": str(data),
                "log_root": str(tmp_path / "log"),
                "workspace_root": str(data / "workspaces"),
                "backup_root": str(data / "backups"),
            },
            "database": {"sqlite_path": str(data / "syntra.db")},
            "metrics": {"enabled": metrics, "port": _port()},
            "backups": {"enabled": False, "retention_days": 30},
        },
        environ={},
    )


def test_real_runtime_thread_and_http_health(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runtime = ServiceRuntime(
        tmp_path / "config.json",
        config_loader=lambda _path: config,
        loop_interval=0.01,
        shutdown_grace=1,
        configure_runtime_logging=False,
    )
    assert runtime.health.projection().ready is False
    runtime.start()
    try:
        assert runtime.scheduler is not None
        assert runtime.scheduler.is_draining is False
        assert runtime.http is not None
        host, port = runtime.http.address
        health = json.loads(urlopen(f"http://{host}:{port}/health").read())
        ready = json.loads(urlopen(f"http://{host}:{port}/ready").read())
        assert health["state"] == "HEALTHY"
        assert ready == {"ready": True}
    finally:
        runtime.stop()
    assert runtime.health.projection().state.value == "DRAINING"


def test_metrics_and_automatic_backups_can_be_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path, metrics=False)
    runtime = ServiceRuntime(
        tmp_path / "config.json",
        config_loader=lambda _path: config,
        loop_interval=0.01,
        shutdown_grace=1,
        configure_runtime_logging=False,
    )
    runtime.start()
    runtime.stop()
    assert runtime.http is None
    assert runtime._backup_thread is None
