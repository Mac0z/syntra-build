from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.domain.codex import CodexProcessStatus, CodexRunRequest
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.infrastructure.codex_runner import (
    DirectProcessLauncher,
    LocalCodexCliRunner,
    sanitised_codex_environment,
)
from syntra_build.infrastructure.persistence import apply_migrations, open_database

NOW = datetime(2026, 9, 23, tzinfo=UTC)
PID, MID, JID = (
    ProjectId(UUID(int=201)),
    MilestoneId(UUID(int=202)),
    JobId(UUID(int=203)),
)
HEAD = "a" * 40


def database(tmp_path: Path, workspace: Path) -> sqlite3.Connection:
    db = open_database(tmp_path / "state.db")
    apply_migrations(db)
    stamp = NOW.isoformat()
    db.execute(
        """INSERT INTO projects
        (id,name,state,created_at,updated_at,last_state_change_at)
        VALUES (?,?,'BUILDING',?,?,?)""",
        (str(PID), "M20", stamp, stamp, stamp),
    )
    db.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,created_at,updated_at)
        VALUES (?,?,20,'M20','Codex','CODING',?,?)""",
        (str(MID), str(PID), stamp, stamp),
    )
    db.execute(
        """INSERT INTO jobs
        (id,project_id,milestone_id,job_type,state,priority,correlation_id,
         attempt_number,max_attempts,timeout_seconds,worker_class,payload_json,
         result_json,created_at,updated_at)
        VALUES (?,?,?,'CODEX','RUNNING',1,'corr',1,1,60,'CODEX','{}','{}',?,?)""",
        (str(JID), str(PID), str(MID), stamp, stamp),
    )
    db.execute(
        """INSERT INTO github_repositories
        (id,project_id,provider,owner,repository_name,full_name,external_repository_id,
         visibility,default_branch,status,created_at,updated_at,verified_at)
        VALUES ('gh',?,'github','o','r','o/r','1','public','main','VERIFIED',?,?,?)""",
        (str(PID), stamp, stamp, stamp),
    )
    db.execute(
        """INSERT INTO git_repositories
        (id,project_id,github_repository_id,repository_path,remote_name,remote_url,
         default_branch,created_at,updated_at)
        VALUES ('repo',?,'gh',?,'origin','https://github.com/o/r.git','main',?,?)""",
        (str(PID), str(tmp_path / "repo.git"), stamp, stamp),
    )
    db.execute(
        """INSERT INTO git_workspaces
        (id,project_id,milestone_id,git_repository_id,branch_name,worktree_path,
         base_branch,base_sha,current_head_sha,state,created_at)
        VALUES ('workspace',?,?,'repo','syntra/m20-codex',?,'main',?,?,'READY',?)""",
        (str(PID), str(MID), str(workspace), HEAD, HEAD, stamp),
    )
    db.commit()
    return db


def executable(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-codex"
    path.write_text("#!/bin/sh\ncat >/dev/null\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def request(workspace: Path, timeout: float = 2) -> CodexRunRequest:
    return CodexRunRequest(
        "1.0",
        "corr",
        PID,
        MID,
        JID,
        1,
        workspace,
        {"objective": "make a disposable change"},
        "Do safe engineering.",
        timeout,
    )


def runner(
    db: sqlite3.Connection,
    tmp_path: Path,
    command: Path,
    *,
    graceful_termination_seconds: float = 2.0,
) -> LocalCodexCliRunner:
    return LocalCodexCliRunner(
        db,
        executable=str(command),
        worker_identity="syntra-codex",
        artifact_root=tmp_path / "artifacts",
        launcher=DirectProcessLauncher(),
        graceful_termination_seconds=graceful_termination_seconds,
    )


def test_environment_is_an_allowlist_without_control_plane_credentials() -> None:
    clean = sanitised_codex_environment(
        {
            "PATH": "/bin",
            "HOME": "/home/syntra-codex",
            "GITHUB_TOKEN": "secret",
            "GH_TOKEN": "secret",
            "TELEGRAM_BOT_TOKEN": "secret",
            "OPENAI_API_KEY": "secret",
            "GIT_ASKPASS": "/secret/helper",
            "SYNTRA_DATABASE": "/secret/state.db",
        }
    )
    assert clean["HOME"] == "/home/syntra-codex"
    assert not (
        {
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "OPENAI_API_KEY",
            "GIT_ASKPASS",
            "SYNTRA_DATABASE",
        }
        & clean.keys()
    )


def test_success_preserves_files_without_commit_and_captures_private_artifacts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", workspace], check=True)
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True
    ).stdout
    command = executable(
        tmp_path, "printf changed > changed.txt\necho provider-output\nexit 0\n"
    )
    with database(tmp_path, workspace) as db:
        result = runner(db, tmp_path, command).run(request(workspace))
        row = db.execute("SELECT * FROM codex_runs").fetchone()
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True
    ).stdout
    assert result.process_status is CodexProcessStatus.SUCCEEDED
    assert (workspace / "changed.txt").read_text() == "changed"
    assert before == after  # no trusted commit or push is triggered
    assert (
        row["process_status"] == "SUCCEEDED"
        and row["worker_identity"] == "syntra-codex"
    )
    assert Path(result.stdout_reference or "").read_text().strip() == "provider-output"
    assert (Path(result.stdout_reference or "").stat().st_mode & 0o077) == 0


def test_failed_process_and_self_report_cannot_override_exit_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = executable(
        tmp_path, "echo SUCCEEDED\necho raw-secret-provider-error >&2\nexit 7\n"
    )
    with database(tmp_path, workspace) as db:
        result = runner(db, tmp_path, command).run(request(workspace))
    assert result.process_status is CodexProcessStatus.FAILED
    assert result.exit_code == 7
    assert "raw-secret-provider-error" not in caplog.text


def test_timeout_kills_process_group_and_persists_timed_out(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child_pid = workspace / "child.pid"
    command = executable(tmp_path, f"sleep 30 &\necho $! > {child_pid}\nwait\n")
    with database(tmp_path, workspace) as db:
        result = runner(db, tmp_path, command, graceful_termination_seconds=0.1).run(
            request(workspace, 0.15)
        )
        status = db.execute("SELECT process_status FROM codex_runs").fetchone()[0]
    assert (
        result.process_status is CodexProcessStatus.TIMED_OUT and status == "TIMED_OUT"
    )
    pid = int(child_pid.read_text())
    status_file = Path(f"/proc/{pid}/status")
    # A container init may briefly retain a terminated orphan as a zombie; it
    # cannot execute and is still proof the child received the group signal.
    assert not status_file.exists() or "State:\tZ" in status_file.read_text()


def test_runner_rejects_unregistered_or_mismatched_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    wrong = tmp_path / "outside"
    wrong.mkdir()
    command = executable(tmp_path, "exit 0\n")
    with (
        database(tmp_path, workspace) as db,
        pytest.raises(ValueError, match="persisted workspace"),
    ):
        runner(db, tmp_path, command).run(request(wrong))
