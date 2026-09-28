"""Small independently managed health and metrics HTTP server."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from syntra_build.application.operational_health import OperationalHealth
from syntra_build.domain.health import HealthState
from syntra_build.infrastructure.metrics import MetricsService


class HealthHTTPServer:
    def __init__(
        self, host: str, port: int, health: OperationalHealth, metrics: MetricsService
    ):
        self._health, self._metrics = health, metrics
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/health":
                    projection = owner._health.projection()
                    status = 503 if projection.state is HealthState.UNHEALTHY else 200
                    self._json(status, owner.health_document())
                elif self.path == "/ready":
                    projection = owner._health.projection()
                    self._json(
                        200 if projection.ready else 503, {"ready": projection.ready}
                    )
                elif self.path == "/metrics":
                    body = owner._metrics.render()
                    self.send_response(HTTPStatus.OK)
                    self.send_header(
                        "Content-Type", "text/plain; version=0.0.4; charset=utf-8"
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._json(404, {"error": "not_found"})

            def _json(self, status: int, value: object) -> None:
                body = json.dumps(value, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return None

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("health HTTP server already started")
        self._thread = Thread(
            target=self._server.serve_forever, name="syntra-health", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def health_document(self) -> dict[str, object]:
        projection = self._health.projection()
        resource = projection.resources
        return {
            "state": projection.state.value,
            "ready": projection.ready,
            "reasons": [reason.value for reason in projection.reasons],
            "resources": {
                "total_bytes": resource.total_bytes,
                "used_bytes": resource.used_bytes,
                "free_bytes": resource.free_bytes,
                "free_percent": resource.free_percent,
                "artifact_bytes": resource.artifact_bytes,
            },
        }
