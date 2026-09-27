from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from uuid import UUID

from syntra_build.application.commands.models import InboundMessage
from syntra_build.application.status import (
    ReadOnlyStatusIntentResolver,
    SQLiteStatusService,
    format_project_status,
)
from syntra_build.domain import ProjectId
from syntra_build.infrastructure.persistence.migrations import apply_migrations
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository

NOW = "2026-09-27T12:00:00+00:00"
PID = "00000000-0000-0000-0000-000000000001"
MID = "00000000-0000-0000-0000-000000000002"


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    apply_migrations(connection)
    return connection


def add_project(
    connection: sqlite3.Connection,
    project_id: str,
    name: str,
    state: str,
    *,
    resume_state: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO projects
        (id,name,state,resume_state,activity,created_at,updated_at,last_state_change_at,
         canonical_name,repository_visibility) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            project_id,
            name,
            state,
            resume_state,
            None,
            NOW,
            NOW,
            NOW,
            name.casefold(),
            "public",
        ),
    )


def add_milestone(connection: sqlite3.Connection, state: str = "CODING") -> None:
    connection.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,created_at,updated_at)
        VALUES (?, ?, 4, 'M04', 'Performance fixes', ?, ?, ?)""",
        (MID, PID, state, NOW, NOW),
    )


def service(connection: sqlite3.Connection) -> SQLiteStatusService:
    return SQLiteStatusService(
        connection, SQLiteProjectRepository(connection, lambda: "unused")
    )


def test_building_codex_retry_and_non_interference() -> None:
    connection = database()
    add_project(connection, PID, "FlowTrack", "BUILDING")
    add_milestone(connection)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        """INSERT INTO git_workspaces
        (id,project_id,milestone_id,git_repository_id,branch_name,worktree_path,
         base_branch,base_sha,state,created_at)
        VALUES ('workspace-1',?,?,'git-repo-1','syntra/m04-performance',
        '/tmp/synthetic-m28-worktree','main',?,'ACTIVE',?)""",
        (PID, MID, "0" * 40, NOW),
    )
    connection.execute(
        """INSERT INTO jobs
        (id,project_id,milestone_id,job_type,state,priority,correlation_id,
         attempt_number,max_attempts,started_at,next_retry_at,worker_class,payload_json,
         result_json,last_error_id,created_at,updated_at,failure_classification)
        VALUES ('job-1',?,?, 'CODEX_REVIEW_REWORK','RETRY_WAIT',0,'correlation',2,3,
        ?,?,'CODEX','{}','{}','error-42',?,?, 'TRANSIENT')""",
        (PID, MID, NOW, "2026-09-27T14:32:00+00:00", NOW, NOW),
    )
    before = connection.iterdump()
    before_dump = tuple(before)
    status = service(connection).project_status(ProjectId(UUID(PID)))
    rendered = format_project_status(status)
    assert status.state.value == "BUILDING"
    assert status.activity == "Codex retry_wait — CODEX_REVIEW_REWORK"
    assert status.milestone and status.milestone.code == "M04"
    assert status.workers[0].attempt == 2
    assert status.branch == "syntra/m04-performance"
    assert "attempt 2/3" in rendered
    assert "TRANSIENT" in rendered
    assert "error-42" in rendered
    assert "Retry is scheduled" in rendered
    assert tuple(connection.iterdump()) == before_dump


def test_active_projects_excludes_waiting_paused_blocked_and_terminal() -> None:
    connection = database()
    states = (
        "BUILDING",
        "READY",
        "WAITING_HUMAN",
        "PAUSED",
        "BLOCKED",
        "COMPLETE",
        "FAILED",
    )
    for index, state in enumerate(states, 1):
        add_project(
            connection,
            f"00000000-0000-0000-0000-{index:012d}",
            state.title(),
            state,
        )
    assert [item.state.value for item in service(connection).active_projects()] == [
        "BUILDING",
        "READY",
    ]


def test_waiting_human_and_feedback_projection() -> None:
    connection = database()
    add_project(connection, PID, "FlowTrack", "WAITING_HUMAN")
    add_milestone(connection, "HUMAN_TEST")
    connection.execute(
        """INSERT INTO human_gates
        (id,project_id,milestone_id,gate_type,state,title,prompt,expected_response_type,
         options_json,created_at,created_by,correlation_id)
        VALUES ('gate-1',?,?,'HUMAN_TEST','NOTIFIED','Test release','Run the checks',
        'HUMAN_TEST','[]',?,'syntra','correlation')""",
        (PID, MID, NOW),
    )
    connection.execute(
        """INSERT INTO m25_telegram_feedback_interactions
        (id,gate_id,outcome,chat_id,user_id,prompt_message_id,state,created_at)
        VALUES ('feedback-1','gate-1','FAIL','chat','user','message',
        'WAITING_FEEDBACK',?)""",
        (NOW,),
    )
    status_service = service(connection)
    actions = status_service.waiting_for_human()
    assert len(actions) == 1
    assert actions[0].project_name == "FlowTrack"
    assert actions[0].milestone_code == "M04"
    assert actions[0].waiting_for_feedback
    projection = status_service.project_status(ProjectId(UUID(PID)))
    assert not projection.workers
    assert projection.activity == "Waiting for human feedback"
    assert projection.next_action == "Waiting for your feedback."


def test_pr_head_ci_and_architect_freshness() -> None:
    connection = database()
    add_project(connection, PID, "FlowTrack", "BUILDING")
    add_milestone(connection, "ARCHITECT_REVIEW")
    connection.execute("PRAGMA foreign_keys=OFF")
    sha_a, sha_b = "a" * 40, "b" * 40
    connection.execute(
        """INSERT INTO pull_requests
        (id,project_id,milestone_id,github_repository_id,external_pr_number,state,
         head_branch,base_branch,head_sha,web_url,title,created_at,updated_at,
         last_reconciled_at) VALUES
        ('pr-1',?,?, 'repo-1',41,'OPEN','syntra/m04','main',?,
         'https://example.invalid/pr/41','M04',?,?,?)""",
        (PID, MID, sha_a, NOW, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO ci_runs
        (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,
         overall_status,started_at,last_checked_at,summary_json,retry_count)
        VALUES ('ci-1',?,?, 'pr-1',?,1,'PASSED',?,?,'{}',0)""",
        (PID, MID, sha_a, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO ci_checks
        (id,ci_run_id,name,external_check_id,status,conclusion)
        VALUES ('check-1','ci-1','tests','external-1','COMPLETED','PASSED')"""
    )
    connection.execute(
        """INSERT INTO architect_reviews
        (id,project_id,milestone_id,architect_request_id,pull_request_id,
         reviewed_sha,verdict,summary,created_at)
        VALUES ('review-1',?,?,'request-1','pr-1',?,'APPROVE','Looks good',?)""",
        (PID, MID, sha_a, NOW),
    )
    current = service(connection).project_status(ProjectId.from_string(PID))
    assert current.pull_request and current.pull_request.head_sha == sha_a
    assert current.ci and current.ci.current and current.ci.status == "PASSED"
    assert current.architect and current.architect.current
    connection.execute(
        "UPDATE pull_requests SET head_sha=?,updated_at=? WHERE id='pr-1'",
        (sha_b, NOW),
    )
    stale = service(connection).project_status(ProjectId.from_string(PID))
    assert stale.ci and not stale.ci.current
    assert stale.architect and not stale.architect.current
    rendered = format_project_status(stale)
    assert rendered.count("stale; not current head") == 2


def test_paused_blocked_failed_and_complete_next_actions() -> None:
    connection = database()
    expectations = {
        "PAUSED": "Resume the project",
        "BLOCKED": "Resolve the recorded blocker",
        "FAILED": "No recovery action",
        "COMPLETE": "No further action",
    }
    for index, (state, expected) in enumerate(expectations.items(), 1):
        project_id = f"00000000-0000-0000-0000-{index:012d}"
        add_project(
            connection,
            project_id,
            state.title(),
            state,
            resume_state="BUILDING" if state == "PAUSED" else None,
        )
        if state == "FAILED":
            connection.execute(
                """INSERT INTO jobs
                (id,project_id,job_type,state,priority,correlation_id,attempt_number,
                 max_attempts,worker_class,payload_json,result_json,last_error_id,
                 created_at,updated_at,failure_classification,retry_exhausted,
                 exhaustion_reason)
                VALUES ('failed-job',?,'CODEX_IMPLEMENT','FAILED',0,'correlation',3,
                3,'CODEX','{}','{}','codex-process-9',?,?,'PERMANENT',1,
                'retry budget exhausted')""",
                (project_id, NOW, NOW),
            )
        projection = service(connection).project_status(
            ProjectId.from_string(project_id)
        )
        assert expected in projection.next_action
        if state == "PAUSED":
            assert (
                projection.resume_state and projection.resume_state.value == "BUILDING"
            )
        if state == "FAILED":
            assert projection.latest_error == "codex-process-9"
            assert projection.workers[0].retry_exhausted


def test_natural_language_resolver_is_status_only() -> None:
    resolver = ReadOnlyStatusIntentResolver()

    def resolve(text: str):  # type: ignore[no-untyped-def]
        return resolver.resolve(
            InboundMessage("telegram", "1", "2", "3", datetime.now(UTC), text, "4")
        )

    assert resolve("What projects are active?").type.value == "ACTIVE_PROJECTS"
    assert resolve("Is anything waiting for me?").type.value == "WAITING"
    command = resolve("What's happening with FlowTrack?")
    assert command.type.value == "PROJECT_STATUS"
    assert command.project_reference == "FlowTrack"
    assert resolve("pause FlowTrack please") is None
    assert resolve("cancel FlowTrack") is None
    assert resolve("tell me a story") is None
