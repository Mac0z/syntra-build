from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from uuid import UUID

from syntra_build.application.commands.models import Command, InboundMessage
from syntra_build.application.commands.router import CommandRouter
from syntra_build.application.commands.services import (
    CommandAuditRequest,
    ProjectCommandResult,
    ProjectSummary,
)
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


def add_milestone_record(
    connection: sqlite3.Connection,
    sequence: int,
    state: str,
    *,
    updated_at: str = NOW,
) -> None:
    milestone_id = f"00000000-0000-0000-1000-{sequence:012d}"
    connection.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (
            milestone_id,
            PID,
            sequence,
            f"M{sequence:02d}",
            f"Milestone {sequence}",
            state,
            NOW,
            updated_at,
        ),
    )


def service(connection: sqlite3.Connection) -> SQLiteStatusService:
    return SQLiteStatusService(
        connection, SQLiteProjectRepository(connection, lambda: "unused")
    )


class _Commands:
    def handle(self, command: Command, project: ProjectSummary) -> ProjectCommandResult:
        raise AssertionError("status must not call project mutations")


class _Audit:
    def record(self, request: CommandAuditRequest) -> None:
        raise AssertionError("status must not create mutation audit records")


class _Health:
    def current_health(self) -> str:
        return "healthy"


def message(text: str, sequence: int) -> InboundMessage:
    return InboundMessage(
        "telegram",
        str(sequence),
        str(sequence),
        "human",
        datetime.now(UTC),
        text,
        "chat",
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


def test_failed_project_ignores_future_pending_milestones() -> None:
    connection = database()
    add_project(connection, PID, "Failed project", "FAILED")
    for sequence, state in (
        (1, "COMPLETE"),
        (2, "FAILED"),
        (3, "PENDING"),
        (4, "PENDING"),
    ):
        add_milestone_record(connection, sequence, state)
    milestone = service(connection).project_status(ProjectId.from_string(PID)).milestone
    assert milestone and (milestone.code, milestone.state) == ("M02", "FAILED")


def test_cancelled_project_ignores_future_pending_milestones() -> None:
    connection = database()
    add_project(connection, PID, "Cancelled project", "CANCELLED")
    add_milestone_record(connection, 1, "COMPLETE")
    add_milestone_record(connection, 2, "CANCELLED")
    add_milestone_record(connection, 3, "PENDING")
    milestone = service(connection).project_status(ProjectId.from_string(PID)).milestone
    assert milestone and (milestone.code, milestone.state) == ("M02", "CANCELLED")


def test_complete_project_reports_latest_completed_milestone() -> None:
    connection = database()
    add_project(connection, PID, "Complete project", "COMPLETE")
    add_milestone_record(connection, 1, "COMPLETE")
    add_milestone_record(connection, 2, "COMPLETE")
    add_milestone_record(connection, 3, "PENDING")
    milestone = service(connection).project_status(ProjectId.from_string(PID)).milestone
    assert milestone and (milestone.code, milestone.state) == ("M02", "COMPLETE")


def test_project_with_only_future_milestones_has_no_current_milestone() -> None:
    connection = database()
    add_project(connection, PID, "Ready project", "READY")
    add_milestone_record(connection, 1, "PENDING")
    add_milestone_record(connection, 2, "PENDING")
    projection = service(connection).project_status(ProjectId.from_string(PID))
    assert projection.milestone is None
    assert projection.activity == "No active work"
    assert "Milestone: No active implementation milestone" in format_project_status(
        projection
    )


def test_active_milestone_wins_over_complete_and_future_milestones() -> None:
    connection = database()
    add_project(connection, PID, "Building project", "BUILDING")
    add_milestone_record(connection, 1, "COMPLETE")
    add_milestone_record(connection, 2, "CODING")
    add_milestone_record(connection, 3, "PENDING")
    milestone = service(connection).project_status(ProjectId.from_string(PID)).milestone
    assert milestone and (milestone.code, milestone.state) == ("M02", "CODING")


def test_waiting_for_me_includes_only_gates_needing_input() -> None:
    connection = database()
    add_project(connection, PID, "FlowTrack", "WAITING_HUMAN")
    add_milestone(connection, "HUMAN_TEST")
    states = (
        "PENDING",
        "NOTIFIED",
        "RESPONDED",
        "VALIDATED",
        "RESOLVED",
        "CANCELLED",
        "EXPIRED",
    )
    for index, state in enumerate(states):
        connection.execute(
            """INSERT INTO human_gates
            (id,project_id,milestone_id,gate_type,state,title,prompt,
             expected_response_type,options_json,created_at,created_by,correlation_id)
            VALUES (?,?,?,'HUMAN_TEST',?,'Test release','Run the checks',
            'HUMAN_TEST','[]',?,'syntra','correlation')""",
            (f"gate-{index}", PID, MID, state, NOW),
        )
    actions = service(connection).waiting_for_human()
    assert [(action.gate_id, action.state) for action in actions] == [
        ("gate-0", "PENDING"),
        ("gate-1", "NOTIFIED"),
    ]


def test_prompting_and_waiting_feedback_are_not_fresh_human_tests() -> None:
    for feedback_state in ("PROMPTING", "WAITING_FEEDBACK"):
        connection = database()
        add_project(connection, PID, "FlowTrack", "WAITING_HUMAN")
        add_milestone(connection, "HUMAN_TEST")
        connection.execute(
            """INSERT INTO human_gates
            (id,project_id,milestone_id,gate_type,state,title,prompt,
             expected_response_type,options_json,created_at,created_by,correlation_id)
            VALUES ('gate-1',?,?,'HUMAN_TEST','RESPONDED','Test release',
            'Run the checks','HUMAN_TEST','[]',?,'syntra','correlation')""",
            (PID, MID, NOW),
        )
        prompt_message_id = "message" if feedback_state == "WAITING_FEEDBACK" else None
        connection.execute(
            """INSERT INTO m25_telegram_feedback_interactions
            (id,gate_id,outcome,chat_id,user_id,prompt_message_id,state,created_at)
            VALUES ('feedback-1','gate-1','FAIL','chat','user',?,?,?)""",
            (prompt_message_id, feedback_state, NOW),
        )
        status_service = service(connection)
        actions = status_service.waiting_for_human()
        assert len(actions) == 1
        assert actions[0].feedback_state == feedback_state
        projection = status_service.project_status(ProjectId(UUID(PID)))
        assert "feedback" in projection.activity.lower()
        assert "test result" not in projection.next_action.lower()


def test_human_test_binding_is_projected_exactly() -> None:
    connection = database()
    add_project(connection, PID, "FlowTrack", "WAITING_HUMAN")
    add_milestone(connection, "HUMAN_TEST")
    connection.execute("PRAGMA foreign_keys=OFF")
    sha = "c" * 40
    connection.execute(
        """INSERT INTO human_gates
        (id,project_id,milestone_id,gate_type,state,title,prompt,expected_response_type,
         options_json,created_at,created_by,correlation_id)
        VALUES ('gate-1',?,?,'HUMAN_TEST','NOTIFIED','Test release','Run checks',
        'HUMAN_TEST','[]',?,'syntra','correlation')""",
        (PID, MID, NOW),
    )
    connection.execute(
        """INSERT INTO ci_runs
        (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,
         overall_status,started_at,last_checked_at,summary_json,retry_count)
        VALUES ('ci-bound',?,?,'pr-bound',?,1,'PASSED',?,?,'{}',0)""",
        (PID, MID, sha, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO human_test_bindings
        (gate_id,project_id,milestone_id,architect_review_id,pull_request_id,
         pull_request_number,tested_head_sha,ci_run_id,artifact_reference,
         test_instructions,created_at)
        VALUES ('gate-1',?,?,'review-bound','pr-bound',41,?,'ci-bound',
        'artifact://release','Run exact acceptance checks',?)""",
        (PID, MID, sha, NOW),
    )
    action = service(connection).waiting_for_human()[0]
    assert action.pull_request_number == 41
    assert action.tested_head_sha == sha
    assert action.ci_run_id == "ci-bound"
    assert action.ci_status == "PASSED"
    assert action.artifact_reference == "artifact://release"
    assert action.test_instructions == "Run exact acceptance checks"
    rendered = format_project_status(
        service(connection).project_status(ProjectId.from_string(PID))
    )
    assert "Human test: PR #41 @ cccccccccc, CI passed" in rendered


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


def test_ci_is_scoped_to_selected_pr_head_and_latest_attempt() -> None:
    connection = database()
    add_project(connection, PID, "FlowTrack", "BUILDING")
    add_milestone(connection, "CI_RUNNING")
    connection.execute("PRAGMA foreign_keys=OFF")
    old_sha, current_sha = "a" * 40, "b" * 40
    for pr_id, number, state, sha in (
        ("pr-old", 40, "CLOSED", old_sha),
        ("pr-current", 41, "OPEN", current_sha),
    ):
        connection.execute(
            """INSERT INTO pull_requests
            (id,project_id,milestone_id,github_repository_id,external_pr_number,state,
             head_branch,base_branch,head_sha,web_url,title,created_at,updated_at,
             last_reconciled_at) VALUES (?,?,?,'repo-1',?,?,?,?,?,?,'M04',?,?,?)""",
            (
                pr_id,
                PID,
                MID,
                number,
                state,
                f"syntra/{pr_id}",
                "main",
                sha,
                f"https://example.invalid/{number}",
                NOW,
                NOW,
                NOW,
            ),
        )
    connection.execute(
        """INSERT INTO ci_runs
        (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,
         overall_status,started_at,last_checked_at,summary_json,retry_count)
        VALUES ('ci-old',?,?,'pr-old',?,1,'PASSED',?,?,'{}',0)""",
        (PID, MID, old_sha, NOW, NOW),
    )
    status_service = service(connection)
    without_current = status_service.project_status(ProjectId.from_string(PID))
    assert without_current.pull_request and without_current.pull_request.number == 41
    assert without_current.ci is None
    for attempt, status in ((1, "FAILED"), (2, "RUNNING")):
        connection.execute(
            """INSERT INTO ci_runs
            (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,
             overall_status,started_at,last_checked_at,summary_json,retry_count)
            VALUES (?,?,?,'pr-current',?,?,?, ?,?,'{}',0)""",
            (
                f"ci-current-{attempt}",
                PID,
                MID,
                current_sha,
                attempt,
                status,
                NOW,
                NOW,
            ),
        )
    current = status_service.project_status(ProjectId.from_string(PID))
    assert current.ci and current.ci.current
    assert current.ci.status == "RUNNING"


def test_architect_blocking_findings_match_gatekeeper_open_semantics() -> None:
    connection = database()
    add_project(connection, PID, "FlowTrack", "BUILDING")
    add_milestone(connection, "ARCHITECT_REVIEW")
    connection.execute("PRAGMA foreign_keys=OFF")
    sha = "a" * 40
    connection.execute(
        """INSERT INTO pull_requests
        (id,project_id,milestone_id,github_repository_id,external_pr_number,state,
         head_branch,base_branch,head_sha,web_url,title,created_at,updated_at,
         last_reconciled_at) VALUES
        ('pr-1',?,?,'repo-1',41,'OPEN','syntra/m04','main',?,
         'https://example.invalid/41','M04',?,?,?)""",
        (PID, MID, sha, NOW, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO architect_reviews
        (id,project_id,milestone_id,architect_request_id,pull_request_id,
         reviewed_sha,verdict,summary,created_at)
        VALUES ('review-a',?,?,'request-a','pr-1',?,'CHANGES_REQUIRED','Fix',?)""",
        (PID, MID, sha, "2026-09-27T12:00:00+00:00"),
    )
    connection.execute(
        """INSERT INTO architect_review_findings
        (id,review_id,finding_code,severity,requirement_ref,description,
         recommended_action,status,created_at)
        VALUES ('finding-a','review-a','F-1','major','M28','Problem','Fix it',
        'OPEN',?)""",
        (NOW,),
    )
    for suffix, status in (
        ("accepted", "ACCEPTED"),
        ("resolved", "RESOLVED"),
        ("superseded", "SUPERSEDED"),
    ):
        connection.execute(
            """INSERT INTO architect_review_findings
            (id,review_id,finding_code,severity,requirement_ref,description,
             recommended_action,status,created_at)
            VALUES (?, 'review-a', ?, 'critical', 'M28', 'Historical', 'Action',
            ?, ?)""",
            (f"finding-{suffix}", f"F-{suffix}", status, NOW),
        )
    connection.execute(
        """INSERT INTO architect_reviews
        (id,project_id,milestone_id,architect_request_id,pull_request_id,
         reviewed_sha,verdict,summary,created_at)
        VALUES ('review-b',?,?,'request-b','pr-1',?,'HUMAN_TEST_REQUIRED',
        'Test it','2026-09-27T13:00:00+00:00')""",
        (PID, MID, sha),
    )
    status_service = service(connection)
    projection = status_service.project_status(ProjectId.from_string(PID))
    # Gatekeeper's merge guard treats only OPEN findings as blocking.
    assert projection.architect and projection.architect.blocking_findings == 1
    connection.execute(
        """INSERT INTO architect_reviews
        (id,project_id,milestone_id,architect_request_id,pull_request_id,
         reviewed_sha,verdict,summary,created_at)
        VALUES ('review-c',?,?,'request-c','pr-1',?,'APPROVE','Approved',
        '2026-09-27T14:00:00+00:00')""",
        (PID, MID, sha),
    )
    connection.execute(
        """UPDATE architect_review_findings SET status='RESOLVED',
        resolved_by_review_id='review-c' WHERE id='finding-a'"""
    )
    resolved = status_service.project_status(ProjectId.from_string(PID))
    assert resolved.architect and resolved.architect.verdict == "APPROVE"
    assert resolved.architect.blocking_findings == 0


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
    assert resolve("resume FlowTrack please") is None
    assert resolve("cancel FlowTrack") is None
    assert resolve("gate gate-1 APPROVE") is None
    assert resolve("create FlowTrack please") is None
    assert resolve("tell me a story") is None


def test_router_status_queries_are_repeatedly_observational() -> None:
    connection = database()
    add_project(connection, PID, "FlowTrack", "BUILDING")
    add_milestone(connection)
    connection.execute(
        """INSERT INTO jobs
        (id,project_id,milestone_id,job_type,state,priority,correlation_id,
         attempt_number,max_attempts,started_at,worker_class,payload_json,result_json,
         created_at,updated_at)
        VALUES ('job-1',?,?,'CODEX_IMPLEMENT','RUNNING',0,'correlation',1,3,?,
        'CODEX','{}','{}',?,?)""",
        (PID, MID, NOW, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO job_attempts
        (id,job_id,attempt_number,state,started_at,result_json)
        VALUES ('attempt-1','job-1',1,'RUNNING',?,'{}')""",
        (NOW,),
    )
    connection.execute(
        """INSERT INTO human_gates
        (id,project_id,milestone_id,gate_type,state,title,prompt,
         expected_response_type,options_json,created_at,created_by,correlation_id)
        VALUES ('gate-1',?,?,'PRODUCT_DECISION','NOTIFIED','Choose','Choose one',
        'OPTION','[]',?,'syntra','correlation')""",
        (PID, MID, NOW),
    )
    status_service = service(connection)
    router = CommandRouter(
        project_queries=status_service,
        project_commands=_Commands(),
        audit_sink=_Audit(),
        health=_Health(),
        status_service=status_service,
        intent_resolver=ReadOnlyStatusIntentResolver(),
    )
    tables = (
        "projects",
        "milestones",
        "jobs",
        "job_attempts",
        "human_gates",
        "state_transitions",
        "merge_attempts",
        "ci_runs",
        "architect_requests",
        "codex_runs",
    )

    def snapshot() -> dict[str, tuple[tuple[object, ...], ...]]:
        return {
            table: tuple(
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            )
            for table in tables
        }

    before = snapshot()
    for iteration in range(3):
        assert (
            "Project: FlowTrack"
            in router.route(message("status FlowTrack", iteration * 3 + 1)).text
        )
        assert (
            "Active projects:"
            in router.route(
                message("What projects are active?", iteration * 3 + 2)
            ).text
        )
        assert (
            "Waiting for you:"
            in router.route(
                message("Is anything waiting for me?", iteration * 3 + 3)
            ).text
        )
    assert snapshot() == before
