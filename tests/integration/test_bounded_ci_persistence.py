"""Offline bounded-CI evidence survives migration, replay, and restart."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import Connection, IntegrityError
from uuid import UUID

import pytest

from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIDiagnosis,
    CIFailureClassification,
    CIOverallStatus,
)
from syntra_build.infrastructure.persistence.ci import (
    CIRunRecord,
    SQLiteCIRepository,
)
from syntra_build.infrastructure.persistence.connection import (
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.migrations import (
    MIGRATIONS,
    apply_migrations,
    current_schema_version,
)

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=5)
HEAD = "a" * 40
SUMMARY = (
    "Required CI has no applicable run for this PR head. "
    "Check the repository workflow and pull_request trigger, then resume CI."
)


def _seed(connection: Connection, identity: int = 1) -> tuple[str, str, str]:
    project, milestone, repository, pr = (
        str(UUID(int=identity * 10 + offset)) for offset in range(4)
    )
    connection.execute(
        """INSERT INTO projects
        (id,name,state,created_at,updated_at,last_state_change_at,canonical_name)
        VALUES (?,?,'BUILDING',?,?,?,?)""",
        (project, f"project-{identity}", *([NOW.isoformat()] * 3), f"p-{identity}"),
    )
    connection.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,created_at,updated_at,
         codex_cycle_count,ci_rework_count,architect_rework_count,
         human_test_rework_count)
        VALUES (?,?,23,'M23','CI','CI_RUNNING',?,?,0,0,0,0)""",
        (milestone, project, NOW.isoformat(), NOW.isoformat()),
    )
    connection.execute(
        """INSERT INTO github_repositories
        (id,project_id,provider,owner,repository_name,full_name,
         external_repository_id,visibility,default_branch,status,
         created_at,updated_at,verified_at)
        VALUES (?,?,'github','owner',?,?,?,'public','main','VERIFIED',?,?,?)""",
        (
            repository,
            project,
            f"repo-{identity}",
            f"owner/repo-{identity}",
            identity,
            *([NOW.isoformat()] * 3),
        ),
    )
    connection.execute(
        """INSERT INTO pull_requests
        (id,project_id,milestone_id,github_repository_id,external_pr_number,state,
         head_branch,base_branch,head_sha,web_url,title,created_at,updated_at,
         last_reconciled_at)
        VALUES (?,?,?,?,12,'OPEN','syntra/m23','main',?,'https://example/pr/12',
                'M23',?,?,?)""",
        (pr, project, milestone, repository, HEAD, *([NOW.isoformat()] * 3)),
    )
    return project, milestone, pr


def _unknown(
    records: SQLiteCIRepository,
    identity: tuple[str, str, str],
    *,
    now: datetime = NOW,
    head_sha: str = HEAD,
    since: datetime | None = NOW,
    deadline: datetime | None = DEADLINE,
    count: int = 1,
) -> CIRunRecord:
    return records.reconcile(
        *identity,
        head_sha,
        CIOverallStatus.UNKNOWN,
        (),
        (),
        CIFailureClassification.CONFIGURATION,
        now,
        next_check_at=deadline,
        unknown_since=since,
        unknown_deadline_at=deadline,
        unknown_observation_count=count,
        diagnosis=CIDiagnosis.NO_APPLICABLE_RUN,
        diagnosis_summary=SUMMARY,
    )


def test_schema_29_unknown_rows_upgrade_without_invented_evidence(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "upgrade.db") as connection:
        apply_migrations(connection, MIGRATIONS[:29])
        identity = _seed(connection)
        connection.execute(
            """INSERT INTO ci_runs
            (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,
             overall_status,started_at,last_checked_at,summary_json,next_check_at)
            VALUES ('old-run',?,?,?,?,1,'UNKNOWN',?,?,'{}',?)""",
            (*identity, HEAD, NOW.isoformat(), NOW.isoformat(), DEADLINE.isoformat()),
        )
        before = tuple(connection.execute("SELECT * FROM ci_runs").fetchone())

        assert apply_migrations(connection) == len(MIGRATIONS)
        assert current_schema_version(connection) == 30
        assert MIGRATIONS[29].name == "030_bounded_unknown_ci"
        assert apply_migrations(connection) == 30
        after = tuple(connection.execute("SELECT * FROM ci_runs").fetchone())
        assert after[: len(before)] == before
        old = SQLiteCIRepository(connection).get("old-run")
        assert old.overall_status is CIOverallStatus.UNKNOWN
        assert old.checks == ()
        assert old.unknown_since is old.unknown_deadline_at is None
        assert old.unknown_observation_count == 0
        assert old.diagnosis is old.diagnosis_summary is None
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_grace_and_actionable_diagnosis_survive_restart_and_identical_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart.db"
    connection = open_database(path)
    apply_migrations(connection)
    identity = _seed(connection)
    with transaction(connection):
        original = _unknown(SQLiteCIRepository(connection), identity)
    connection.close()

    with open_database(path) as reopened:
        records = SQLiteCIRepository(reopened)
        assert records.get(original.id) == original
        with transaction(reopened):
            repeated = _unknown(records, identity)
        assert repeated == original
        assert records.latest_for_head(identity[2], HEAD) == original
        assert original.unknown_since == NOW
        assert original.unknown_deadline_at == DEADLINE
        assert original.unknown_observation_count == 1
        assert original.diagnosis is CIDiagnosis.NO_APPLICABLE_RUN
        assert original.diagnosis_summary == SUMMARY
        assert original.overall_status is CIOverallStatus.UNKNOWN
        assert reopened.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 1
        assert reopened.execute("SELECT count(*) FROM ci_checks").fetchone()[0] == 0


def test_provider_failure_preserves_successful_absence_window_and_counter(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "provider.db") as connection:
        apply_migrations(connection)
        identity = _seed(connection)
        records = SQLiteCIRepository(connection)
        first = _unknown(records, identity)
        failure = records.reconcile(
            *identity,
            HEAD,
            CIOverallStatus.UNKNOWN,
            (),
            (),
            CIFailureClassification.TRANSIENT_INFRASTRUCTURE,
            NOW + timedelta(seconds=30),
            next_check_at=NOW + timedelta(seconds=60),
            unknown_since=first.unknown_since,
            unknown_deadline_at=first.unknown_deadline_at,
            unknown_observation_count=first.unknown_observation_count,
            diagnosis=CIDiagnosis.PROVIDER_UNAVAILABLE,
            diagnosis_summary="GitHub CI observation failed; retry provider access.",
        )
        assert failure.id == first.id
        assert failure.unknown_since == NOW
        assert failure.unknown_deadline_at == DEADLINE
        assert failure.unknown_observation_count == 1
        assert failure.diagnosis is CIDiagnosis.PROVIDER_UNAVAILABLE
        assert failure.overall_status is CIOverallStatus.UNKNOWN


@pytest.mark.parametrize(
    "status", [CIOverallStatus.QUEUED, CIOverallStatus.RUNNING, CIOverallStatus.PASSED]
)
def test_real_ci_progress_clears_absence_window(
    tmp_path: Path, status: CIOverallStatus
) -> None:
    with open_database(tmp_path / "progress.db") as connection:
        apply_migrations(connection)
        identity = _seed(connection)
        records = SQLiteCIRepository(connection)
        absent = _unknown(records, identity)
        check = CICheck(
            "validate",
            "job",
            CICheckStatus.COMPLETED
            if status is CIOverallStatus.PASSED
            else CICheckStatus.RUNNING,
            CICheckConclusion.PASSED if status is CIOverallStatus.PASSED else None,
        )
        progressed = records.reconcile(
            *identity,
            HEAD,
            status,
            (check,),
            ("run",),
            None,
            NOW + timedelta(seconds=30),
        )
        assert progressed.id == absent.id
        assert progressed.unknown_since is progressed.unknown_deadline_at is None
        assert progressed.unknown_observation_count == 0
        assert progressed.diagnosis is progressed.diagnosis_summary is None
        assert progressed.overall_status is status


def test_grace_window_and_counter_cannot_regress_during_absence(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "immutable.db") as connection:
        apply_migrations(connection)
        identity = _seed(connection)
        records = SQLiteCIRepository(connection)
        first = _unknown(records, identity, count=2)
        with pytest.raises(IntegrityError, match="immutable"):
            _unknown(records, identity, deadline=DEADLINE + timedelta(minutes=5))
        with pytest.raises(IntegrityError, match="immutable"):
            _unknown(records, identity, since=NOW + timedelta(seconds=1))
        with pytest.raises(IntegrityError, match="cannot regress"):
            _unknown(records, identity, count=1)
        assert records.get(first.id) == first


def test_deadline_is_utc_and_invalid_window_fails_before_write(tmp_path: Path) -> None:
    with open_database(tmp_path / "timestamp.db") as connection:
        apply_migrations(connection)
        identity = _seed(connection)
        records = SQLiteCIRepository(connection)
        with pytest.raises(IntegrityError, match="both timestamps"):
            _unknown(records, identity, deadline=None)
        with pytest.raises(IntegrityError, match="timezone-aware"):
            _unknown(records, identity, since=NOW.replace(tzinfo=None))
        with pytest.raises(IntegrityError, match="precedes"):
            _unknown(records, identity, deadline=NOW - timedelta(seconds=1))
        with pytest.raises(IntegrityError, match="negative"):
            _unknown(records, identity, count=-1)
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 0

        offset = timezone(timedelta(hours=2))
        first = _unknown(
            records,
            identity,
            since=NOW.astimezone(offset),
            deadline=DEADLINE.astimezone(offset),
        )
        assert records.get(first.id).unknown_since == NOW
        row = connection.execute("SELECT * FROM ci_runs").fetchone()
        assert row["unknown_since"] == NOW.isoformat()
        assert row["unknown_deadline_at"] == DEADLINE.isoformat()


def test_new_head_has_independent_window_and_foreign_owner_cannot_mutate(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "ownership.db") as connection:
        apply_migrations(connection)
        identity = _seed(connection)
        other = _seed(connection, 2)
        records = SQLiteCIRepository(connection)
        first = _unknown(records, identity, count=2)
        later = NOW + timedelta(minutes=1)
        second = _unknown(
            records,
            identity,
            head_sha="b" * 40,
            now=later,
            since=later,
            deadline=later + timedelta(minutes=5),
        )
        assert first.id != second.id
        assert second.unknown_since == later
        assert second.unknown_observation_count == 1
        assert records.get(first.id) == first
        with pytest.raises(IntegrityError, match="ownership"):
            _unknown(records, (other[0], other[1], identity[2]))
        assert records.get(first.id) == first
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 2


def test_expired_unknown_diagnosis_does_not_create_passing_or_duplicate_evidence(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "blocked.db") as connection:
        apply_migrations(connection)
        identity = _seed(connection)
        records = SQLiteCIRepository(connection)
        first = _unknown(records, identity)
        for _ in range(2):
            blocked = records.reconcile(
                *identity,
                HEAD,
                CIOverallStatus.UNKNOWN,
                (),
                (),
                CIFailureClassification.CONFIGURATION,
                DEADLINE,
                unknown_since=NOW,
                unknown_deadline_at=DEADLINE,
                unknown_observation_count=2,
                diagnosis=CIDiagnosis.NO_APPLICABLE_RUN,
                diagnosis_summary=SUMMARY,
            )
        assert blocked.id == first.id
        assert blocked.overall_status is CIOverallStatus.UNKNOWN
        assert blocked.next_check_at is None
        assert blocked.diagnosis_summary == SUMMARY
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM ci_checks").fetchone()[0] == 0
