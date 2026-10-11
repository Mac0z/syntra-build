"""Production CI composition consumes the central missing-check configuration."""

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import NoReturn, cast

from test_missing_ci_monitor import HEAD, Actions, FrozenClock, PullRequests, _seed

from syntra_build.application.codex import WorkspaceBoundCodexRunner
from syntra_build.application.production import (
    ProductionDependencies,
    build_production_executors,
)
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
)
from syntra_build.domain.ci import CIDiagnosis, CIOverallStatus
from syntra_build.domain.identifiers import JobId
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence.ci import SQLiteCIRepository
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.migrations import apply_migrations


class _NoArchitectWork:
    provider_name = "unused-offline-provider"
    model = "unused-offline-model"

    def design(self, request: object) -> NoReturn:
        raise AssertionError("missing CI must not request Architect design work")

    def draft_specification(self, request: object) -> NoReturn:
        raise AssertionError("missing CI must not request specification work")

    def review(self, request: object) -> NoReturn:
        raise AssertionError("missing CI must not request Architect review")

    def task(self, request: object) -> NoReturn:
        raise AssertionError("missing CI must not request Architect task work")

    def telemetry(self) -> NoReturn:
        raise AssertionError("missing CI must not invoke Architect work")


def _unexpected_codex(connection: sqlite3.Connection) -> WorkspaceBoundCodexRunner:
    raise AssertionError("missing CI must not request Codex work")


def test_production_ci_route_applies_custom_grace_and_observation_limit(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "syntra.db"
    clock = FrozenClock()
    with open_database(database) as connection:
        apply_migrations(connection)
        project, milestone, pull_request_id = _seed(connection, clock)

    config = load_config(
        {
            "filesystem": {
                "application_root": tmp_path / "app",
                "configuration_root": tmp_path / "etc",
                "data_root": data,
                "log_root": tmp_path / "logs",
            },
            "database": {"sqlite_path": database},
            "ci": {"missing_grace_seconds": 17.5, "missing_max_observations": 1},
        },
        environ={},
    )
    actions = Actions()
    pull_requests = PullRequests()
    routes = build_production_executors(
        config,
        dependencies=ProductionDependencies(
            architect_provider_factory=_NoArchitectWork,
            codex_runner_factory=_unexpected_codex,
            trusted_git=TrustedGit(data / "authentication"),
            github_pull_requests=pull_requests,
            github_actions_factory=lambda: actions,
        ),
    )
    job = Job(
        JobId.generate(),
        project,
        "CI_RECONCILE",
        JobState.QUEUED,
        0,
        clock(),
        clock(),
        milestone,
        correlation_id="configured-missing-ci",
        worker_class=WorkerClass.CI,
    )

    result = cast(JobExecutionResult, routes[WorkerClass.CI].execute(job))

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert actions.calls == [("owner/one", 12, HEAD)]
    assert pull_requests.calls == 2
    with open_database(database) as connection:
        record = SQLiteCIRepository(connection).latest_for_head(pull_request_id, HEAD)
        assert record is not None
        assert record.overall_status is CIOverallStatus.UNKNOWN
        assert record.diagnosis is CIDiagnosis.NO_APPLICABLE_RUN
        assert record.unknown_observation_count == 1
        assert record.next_check_at is None
        assert record.unknown_since is not None
        assert record.unknown_deadline_at == record.unknown_since + timedelta(
            seconds=17.5
        )
        state = connection.execute(
            "SELECT state,resume_state FROM milestones WHERE id=?", (str(milestone),)
        ).fetchone()
        assert tuple(state) == ("BLOCKED", "CI_RUNNING")
        assert (
            connection.execute("SELECT COUNT(*) FROM architect_reviews").fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM codex_runs").fetchone()[0] == 0
