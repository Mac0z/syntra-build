"""Auditable local-process implementation of the provider-neutral Codex runner."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from syntra_build.domain.codex import (
    CodexProcessStatus,
    CodexRunRequest,
    CodexRunResult,
)
from syntra_build.infrastructure.persistence.codex import SQLiteCodexRunRepository

_MAX_PROMPT_BYTES = 1_048_576
_ENV_ALLOWLIST = frozenset({"LANG", "LC_ALL", "PATH", "TERM", "TMPDIR"})


class ProcessLauncher(Protocol):
    """Host-specific, narrow identity transition seam."""

    def command(self, executable: str, worktree: Path) -> Sequence[str]: ...

    def cleanup_command(self, worktree: Path) -> Sequence[str] | None: ...


class SudoCodexLauncher:
    """Invoke the root-owned, input-validating deployment helper via sudo."""

    def __init__(
        self, helper: Path = Path("/usr/local/libexec/syntra-codex-launch")
    ) -> None:
        self.helper = helper

    def command(self, executable: str, worktree: Path) -> Sequence[str]:
        return ("sudo", "-n", str(self.helper), str(worktree), executable)

    def cleanup_command(self, worktree: Path) -> Sequence[str]:
        return ("sudo", "-n", str(self.helper), "--cleanup", str(worktree))


class DirectProcessLauncher:
    """Non-privileged test/development seam; never changes OS identity."""

    def command(self, executable: str, worktree: Path) -> Sequence[str]:
        del worktree
        return (executable,)

    def cleanup_command(self, worktree: Path) -> None:
        del worktree
        return None


def sanitised_codex_environment(
    source: Mapping[str, str], overrides: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build an allowlisted environment; overrides cannot enlarge the allowlist."""
    clean = {key: value for key, value in source.items() if key in _ENV_ALLOWLIST}
    if overrides:
        clean.update(
            {key: value for key, value in overrides.items() if key in _ENV_ALLOWLIST}
        )
    clean.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    clean["GIT_TERMINAL_PROMPT"] = "0"
    clean["GIT_CONFIG_NOSYSTEM"] = "1"
    return clean


class LocalCodexCliRunner:
    """Run Codex in one verified worktree; success conveys no Git authority."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        executable: str,
        worker_identity: str,
        artifact_root: Path,
        launcher: ProcessLauncher | None = None,
        environment: Mapping[str, str] | None = None,
        graceful_termination_seconds: float = 2.0,
    ) -> None:
        self._connection = connection
        self._records = SQLiteCodexRunRepository(connection)
        self._executable = executable
        self._worker_identity = worker_identity
        self._artifact_root = artifact_root.resolve()
        self._launcher = launcher or SudoCodexLauncher()
        self._environment = sanitised_codex_environment(environment or os.environ)
        self._grace = graceful_termination_seconds
        self._cancellations: dict[tuple[str, int], threading.Event] = {}
        self._lock = threading.Lock()

    def cancel(self, job_id: str, attempt_number: int) -> bool:
        with self._lock:
            event = self._cancellations.get((job_id, attempt_number))
            if event is None:
                return False
            event.set()
            return True

    def run(self, request: CodexRunRequest) -> CodexRunResult:
        worktree_id = self._authoritative_worktree(request)
        prompt = self._prompt(request)
        encoded = prompt.encode()
        if len(encoded) > _MAX_PROMPT_BYTES:
            raise ValueError("Codex task input exceeds the bounded prompt size")
        run_id = str(uuid4())
        directory = self._artifact_directory(run_id, request.attempt_number)
        stdout_path, stderr_path = directory / "stdout.log", directory / "stderr.log"
        started = datetime.now(UTC)
        task_json = json.dumps(request.task, sort_keys=True, separators=(",", ":"))
        self._records.start(
            run_id,
            request,
            worktree_id,
            hashlib.sha256(task_json.encode()).hexdigest(),
            started,
            str(stdout_path),
            str(stderr_path),
        )
        cancellation = threading.Event()
        key = (str(request.job_id), request.attempt_number)
        with self._lock:
            if key in self._cancellations:
                raise RuntimeError("Codex attempt is already running")
            self._cancellations[key] = cancellation

        process: subprocess.Popen[bytes] | None = None
        status = CodexProcessStatus.FAILED
        exit_code: int | None = None
        try:
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                stdout_path.chmod(0o600)
                stderr_path.chmod(0o600)
                process = subprocess.Popen(
                    [
                        *self._launcher.command(
                            self._executable, request.worktree_path
                        ),
                        "exec",
                        "-",
                    ],
                    cwd=request.worktree_path,
                    env=self._environment,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                self._records.record_process(run_id, process.pid, self._worker_identity)
                assert process.stdin is not None
                try:
                    process.stdin.write(encoded)
                    process.stdin.close()
                except BrokenPipeError:
                    pass
                deadline = time.monotonic() + request.timeout_seconds
                while process.poll() is None:
                    if cancellation.wait(0.05):
                        status = CodexProcessStatus.CANCELLED
                        self._terminate_tree(process)
                        break
                    if time.monotonic() >= deadline:
                        status = CodexProcessStatus.TIMED_OUT
                        self._terminate_tree(process)
                        break
                exit_code = process.wait()
                if status not in {
                    CodexProcessStatus.CANCELLED,
                    CodexProcessStatus.TIMED_OUT,
                }:
                    status = (
                        CodexProcessStatus.SUCCEEDED
                        if exit_code == 0
                        else CodexProcessStatus.FAILED
                    )
        except OSError:
            # Executable, identity-transition, or local capacity failures are
            # normalised without copying provider details into application logs.
            status = CodexProcessStatus.FAILED
        finally:
            cleanup = self._launcher.cleanup_command(request.worktree_path)
            if cleanup is not None:
                try:
                    subprocess.run(
                        cleanup,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=self._grace,
                    )
                except OSError, subprocess.TimeoutExpired:
                    # A stale ACL is reconciled under the global helper lock on
                    # the next launch; M27 can also retry explicit cleanup.
                    pass
            with self._lock:
                self._cancellations.pop(key, None)
        completed = datetime.now(UTC)
        result = CodexRunResult(
            request.interface_version,
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.job_id,
            request.attempt_number,
            status,
            started,
            completed,
            self._worker_identity,
            request.timeout_seconds,
            process.pid if process else None,
            exit_code,
            str(stdout_path),
            str(stderr_path),
            self._safe_summary(status),
        )
        self._records.complete(run_id, result)
        return result

    def _authoritative_worktree(self, request: CodexRunRequest) -> str:
        row = self._connection.execute(
            """SELECT id,worktree_path FROM git_workspaces
            WHERE project_id=? AND milestone_id=?""",
            (str(request.project_id), str(request.milestone_id)),
        ).fetchone()
        if (
            row is None
            or Path(row["worktree_path"]).resolve() != request.worktree_path.resolve()
        ):
            raise ValueError("Codex request does not match a persisted workspace")
        return str(row["id"])

    def _artifact_directory(self, run_id: str, attempt: int) -> Path:
        directory = (
            self._artifact_root / "codex" / run_id / f"attempt-{attempt}"
        ).resolve()
        if self._artifact_root not in directory.parents:
            raise ValueError("artifact path escapes configured root")
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        directory.chmod(0o700)
        return directory

    def _terminate_tree(self, process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self._grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    @staticmethod
    def _safe_summary(status: CodexProcessStatus) -> str:
        return {
            CodexProcessStatus.SUCCEEDED: (
                "Codex process completed successfully; changes are unvalidated."
            ),
            CodexProcessStatus.FAILED: "Codex process failed; see protected artifacts.",
            CodexProcessStatus.TIMED_OUT: (
                "Codex process timed out and its process group was terminated."
            ),
            CodexProcessStatus.CANCELLED: (
                "Codex process was cancelled and its process group was terminated."
            ),
            CodexProcessStatus.ABANDONED: "Codex process was abandoned.",
            CodexProcessStatus.RUNNING: "Codex process is running.",
        }[status]

    @staticmethod
    def _prompt(request: CodexRunRequest) -> str:
        prior = request.previous_run_summary or "None"
        return (
            "You are an untrusted implementation worker. Syntra owns workflow "
            "and Git authority.\n"
            f"Assigned workspace: {request.worktree_path}\n"
            "Edit and test only inside that workspace. Do not commit, push, "
            "create a PR, or merge. "
            "GitHub and control-plane credentials are unavailable by design.\n\n"
            "Structured Architect task:\n"
            f"{json.dumps(request.task, sort_keys=True)}\n\n"
            f"Approved engineering instructions:\n{request.agents_markdown}\n\n"
            f"Previous run context:\n{prior}\n"
        )
