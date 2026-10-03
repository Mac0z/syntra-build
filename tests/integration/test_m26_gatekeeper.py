# mypy: disable-error-code="no-untyped-def,no-any-return"
# ruff: noqa: E501
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build import m26_smoke
from syntra_build.application.gatekeeper import (
    Gatekeeper,
    MergeIdentityError,
)
from syntra_build.application.provisioning import AmbiguousGitHubResult
from syntra_build.application.security import SecurityPolicy
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.merges import (
    MERGE_INTERFACE_VERSION,
    MergeEligibilityRequest,
    MergeRequest,
    MergeResult,
    MergeStatus,
    MergeStrategy,
)
from syntra_build.domain.pull_requests import (
    PULL_REQUEST_INTERFACE_VERSION,
    PullRequestDescriptor,
    PullRequestState,
)
from syntra_build.domain.security import SecurityEventType, SecuritySeverity
from syntra_build.infrastructure.persistence import apply_migrations, open_database

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
SHA = "a" * 40
OTHER_SHA = "b" * 40
MERGE_SHA = "c" * 40


def uid() -> str:
    return str(uuid4())


@dataclass
class Seed:
    project: ProjectId
    milestone: MilestoneId
    repo: str
    pr: str
    ci: str
    review: str
    request: MergeEligibilityRequest


class FakeGitHub:
    def __init__(self, live: PullRequestDescriptor) -> None:
        self.live = live
        self.get_queue: list[PullRequestDescriptor | Exception] = []
        self.merge_result: MergeResult | Exception = MergeResult(
            MERGE_INTERFACE_VERSION,
            live.project_id,
            live.milestone_id,
            live.pull_request_number,
            MergeStatus.MERGED,
            MERGE_SHA,
        )
        self.get_calls: list[tuple[str, int]] = []
        self.merge_calls: list[tuple[str, MergeRequest]] = []
        self.before_merge: object | None = None

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        self.get_calls.append((repository_full_name, number))
        if self.get_queue:
            item = self.get_queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self.live

    def merge(self, repository_full_name: str, request: MergeRequest) -> MergeResult:
        self.merge_calls.append((repository_full_name, request))
        if callable(self.before_merge):
            self.before_merge()
        if isinstance(self.merge_result, Exception):
            raise self.merge_result
        return self.merge_result


@pytest.fixture
def db(tmp_path: Path):
    connection = open_database(tmp_path / "m26.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def seed_eligible(
    db: sqlite3.Connection,
    *,
    project_state: str = "BUILDING",
    milestone_state: str = "MERGE_READY",
    ci_sha: str = SHA,
    ci_status: str | None = "PASSED",
) -> tuple[Seed, FakeGitHub]:
    ids = [uid() for _ in range(8)]
    project, milestone, repo, pr, ci, architect_request, response, review = ids
    stamp = NOW.isoformat()
    db.execute(
        """INSERT INTO projects
        (id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility)
        VALUES (?,?,?, ?,?,?,?,'public')""",
        (project, "M26", project_state, stamp, stamp, stamp, "m26"),
    )
    db.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,created_at,updated_at,
         definition_json,automated_acceptance_json)
        VALUES (?,?,26,'M26','Gatekeeper',?,?,?,'{}','[]')""",
        (milestone, project, milestone_state, stamp, stamp),
    )
    db.execute(
        """INSERT INTO github_repositories VALUES
        (?,?,'github','owner','repo','owner/repo',42,'public','main','VERIFIED',?,?,?)""",
        (repo, project, stamp, stamp, stamp),
    )
    db.execute(
        """INSERT INTO pull_requests
        (id,project_id,milestone_id,github_repository_id,external_pr_number,state,
         head_branch,base_branch,head_sha,web_url,title,created_at,updated_at,last_reconciled_at)
        VALUES (?,?,?,?,7,'OPEN','feature','main',?,'https://github.com/owner/repo/pull/7','M26',?,?,?)""",
        (pr, project, milestone, repo, SHA, stamp, stamp, stamp),
    )
    db.execute(
        "UPDATE milestones SET active_pull_request_id=? WHERE id=?", (pr, milestone)
    )
    if ci_status is not None:
        db.execute(
            """INSERT INTO ci_runs
        (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,
         overall_status,started_at,completed_at,last_checked_at,summary_json,retry_count)
        VALUES (?,?,?,?,?,1,?,?,?,?,'{}',0)""",
            (ci, project, milestone, pr, ci_sha, ci_status, stamp, stamp, stamp),
        )
    db.execute(
        """INSERT INTO architect_requests
        (id,project_id,milestone_id,request_type,provider,model,reasoning_level,
         request_schema_version,request_payload_json,correlation_id,started_at,
         completed_at,status) VALUES (?,?,?,'REVIEW','fake','model','high','1.0',
         '{}','corr',?,?,'SUCCEEDED')""",
        (architect_request, project, milestone, stamp, stamp),
    )
    db.execute(
        """INSERT INTO architect_responses
        (id,architect_request_id,response_type,response_schema_version,
         normalised_payload_json,status,created_at,validation_status,provider,model)
         VALUES (?,?,'REVIEW','1.0','{}','ACCEPTED',?,'VALID','fake','model')""",
        (response, architect_request, stamp),
    )
    db.execute(
        """INSERT INTO architect_reviews
        (id,project_id,milestone_id,architect_request_id,pull_request_id,
         reviewed_sha,verdict,summary,created_at) VALUES (?,?,?,?,?,?,'APPROVE','ok',?)""",
        (review, project, milestone, architect_request, pr, SHA, stamp),
    )
    pid, mid = ProjectId.from_string(project), MilestoneId.from_string(milestone)
    request = MergeEligibilityRequest(
        MERGE_INTERFACE_VERSION,
        "corr",
        pid,
        mid,
        repo,
        42,
        pr,
        7,
        "feature",
        "main",
        SHA,
    )
    live = PullRequestDescriptor(
        PULL_REQUEST_INTERFACE_VERSION,
        pid,
        mid,
        42,
        7,
        PullRequestState.OPEN,
        "feature",
        "main",
        SHA,
        "https://github.com/owner/repo/pull/7",
    )
    return Seed(pid, mid, repo, pr, ci, review, request), FakeGitHub(live)


def guard(result, name: str) -> bool:
    return next(item.passed for item in result.guards if item.guard == name)


def test_fully_eligible_result_is_durable_with_exact_evidence(db) -> None:
    seed, github = seed_eligible(db)
    result = Gatekeeper(db, github).evaluate(seed.request, now=NOW)
    assert result.eligible
    assert result.evidence.ci_run_id == seed.ci
    assert result.evidence.architect_review_id == seed.review
    row = db.execute(
        "SELECT * FROM merge_eligibility_results WHERE id=?", (result.result_id,)
    ).fetchone()
    assert row["interface_version"] == MERGE_INTERFACE_VERSION
    assert json.loads(row["evidence_json"])["ci_run_id"] == seed.ci


@pytest.mark.parametrize(
    ("change", "guard_name"),
    [
        (
            lambda db, s, g: db.execute(
                "UPDATE projects SET state='PAUSED' WHERE id=?", (str(s.project),)
            ),
            "project_state",
        ),
        (
            lambda db, s, g: db.execute(
                "UPDATE projects SET state='CANCELLED' WHERE id=?", (str(s.project),)
            ),
            "project_state",
        ),
        (
            lambda db, s, g: db.execute(
                "UPDATE projects SET state='BLOCKED' WHERE id=?", (str(s.project),)
            ),
            "project_state",
        ),
        (
            lambda db, s, g: db.execute(
                "UPDATE milestones SET state='ARCHITECT_REVIEW' WHERE id=?",
                (str(s.milestone),),
            ),
            "milestone_state",
        ),
        (
            lambda db, s, g: setattr(
                g, "live", replace(g.live, state=PullRequestState.CLOSED)
            ),
            "live_open",
        ),
        (
            lambda db, s, g: setattr(g, "live", replace(g.live, repository_id=99)),
            "live_repository",
        ),
        (
            lambda db, s, g: setattr(g, "live", replace(g.live, pull_request_number=8)),
            "live_pull_request",
        ),
        (
            lambda db, s, g: setattr(g, "live", replace(g.live, head_branch="wrong")),
            "live_branches",
        ),
        (
            lambda db, s, g: setattr(g, "live", replace(g.live, base_branch="release")),
            "live_branches",
        ),
        (
            lambda db, s, g: setattr(g, "live", replace(g.live, head_sha=OTHER_SHA)),
            "live_head",
        ),
        (
            lambda db, s, g: db.execute(
                "UPDATE pull_requests SET head_sha=? WHERE id=?", (OTHER_SHA, s.pr)
            ),
            "persisted_head",
        ),
        (
            lambda db, s, g: db.execute(
                "UPDATE ci_runs SET overall_status='FAILED' WHERE id=?", (s.ci,)
            ),
            "required_ci",
        ),
        (
            lambda db, s, g: db.execute(
                "UPDATE architect_reviews SET superseded_at=? WHERE id=?",
                (NOW.isoformat(), s.review),
            ),
            "architect_approval",
        ),
        (
            lambda db, s, g: db.execute(
                "UPDATE architect_reviews SET verdict='CHANGES_REQUIRED' WHERE id=?",
                (s.review,),
            ),
            "architect_approval",
        ),
    ],
)
def test_gatekeeper_negative_guards_are_durable(db, change, guard_name: str) -> None:
    seed, github = seed_eligible(db)
    change(db, seed, github)
    result = Gatekeeper(db, github).evaluate(seed.request, now=NOW)
    assert not result.eligible and not guard(result, guard_name)
    assert (
        db.execute(
            "SELECT eligible FROM merge_eligibility_results WHERE id=?",
            (result.result_id,),
        ).fetchone()[0]
        == 0
    )
    assert github.merge_calls == []


def test_stale_ci_sha_is_rejected_without_mutating_pr_sha(db) -> None:
    seed, github = seed_eligible(db, ci_sha=OTHER_SHA)
    result = Gatekeeper(db, github).evaluate(seed.request, now=NOW)
    assert not guard(result, "required_ci")


def test_missing_ci_is_rejected(db) -> None:
    seed, github = seed_eligible(db, ci_status=None)
    assert not guard(
        Gatekeeper(db, github).evaluate(seed.request, now=NOW), "required_ci"
    )


def test_architect_approval_for_prior_sha_is_stale(db) -> None:
    seed, github = seed_eligible(db)
    db.execute("UPDATE pull_requests SET head_sha=? WHERE id=?", (OTHER_SHA, seed.pr))
    db.execute(
        """INSERT INTO ci_runs
        (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,
         overall_status,started_at,completed_at,last_checked_at,summary_json,retry_count)
        VALUES (?,?,?,?,?,2,'PASSED',?,?,?,'{}',0)""",
        (
            uid(),
            str(seed.project),
            str(seed.milestone),
            seed.pr,
            OTHER_SHA,
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    github.live = replace(github.live, head_sha=OTHER_SHA)
    current = replace(seed.request, expected_head_sha=OTHER_SHA)
    result = Gatekeeper(db, github).evaluate(current, now=NOW)
    assert guard(result, "required_ci") and not guard(result, "architect_approval")


def test_open_architect_finding_blocks(db) -> None:
    seed, github = seed_eligible(db)
    db.execute(
        """INSERT INTO architect_review_findings VALUES
        (?,?,'blocking','major','M26','bad','fix','OPEN',NULL,?)""",
        (uid(), seed.review, NOW.isoformat()),
    )
    result = Gatekeeper(db, github).evaluate(seed.request, now=NOW)
    assert not guard(result, "blocking_findings")


def add_decision_gate(db, seed: Seed, gate_type: str, state: str) -> str:
    gate = uid()
    resolved = NOW.isoformat() if state == "RESOLVED" else None
    db.execute(
        """INSERT INTO human_gates
        (id,project_id,milestone_id,gate_type,state,title,prompt,
         expected_response_type,options_json,created_at,resolved_at,created_by,correlation_id)
        VALUES (?,?,?,?,?,'Decision','Choose','OPTION','["YES"]',?,?,'SYSTEM','corr')""",
        (
            gate,
            str(seed.project),
            str(seed.milestone),
            gate_type,
            state,
            NOW.isoformat(),
            resolved,
        ),
    )
    if state == "RESOLVED":
        db.execute(
            """INSERT INTO human_gate_responses VALUES
            (?,?,'message','YES','yes','YES','[]','human',?,1,'valid')""",
            (uid(), gate, NOW.isoformat()),
        )
    return gate


@pytest.mark.parametrize("gate_type", ["PRODUCT_DECISION", "TECHNICAL_DECISION"])
@pytest.mark.parametrize("state", ["PENDING", "CANCELLED", "EXPIRED"])
def test_non_resolved_decision_gates_block(db, gate_type: str, state: str) -> None:
    seed, github = seed_eligible(db)
    add_decision_gate(db, seed, gate_type, state)
    assert not guard(
        Gatekeeper(db, github).evaluate(seed.request, now=NOW), "human_gates"
    )


def test_resolved_product_decision_succeeds_and_is_audited(db) -> None:
    seed, github = seed_eligible(db)
    gate = add_decision_gate(db, seed, "PRODUCT_DECISION", "RESOLVED")
    result = Gatekeeper(db, github).evaluate(seed.request, now=NOW)
    assert result.eligible and gate in result.evidence.human_gate_ids
    assert result.evidence.human_response_ids


def add_human_test(
    db, seed: Seed, state: str = "RESOLVED", outcome: str = "PASS", sha: str = SHA
) -> None:
    gate, response, result = uid(), uid(), uid()
    stamp = NOW.isoformat()
    db.execute(
        """INSERT INTO human_gates
        (id,project_id,milestone_id,gate_type,state,title,prompt,expected_response_type,
         options_json,created_at,notified_at,responded_at,resolved_at,created_by,
         correlation_id,architect_review_id) VALUES (?,?,?,'HUMAN_TEST',?,'Test','Run',
         'HUMAN_TEST','["PASS","FAIL","BLOCKED"]',?,?,?,?, 'ARCHITECT','corr',?)""",
        (
            gate,
            str(seed.project),
            str(seed.milestone),
            state,
            stamp,
            stamp,
            stamp if state == "RESOLVED" else None,
            stamp if state == "RESOLVED" else None,
            seed.review,
        ),
    )
    if state == "RESOLVED":
        db.execute(
            "INSERT INTO human_gate_responses VALUES (?,?, 'msg',?,'evidence',?,'[]','human',?,1,'valid')",
            (response, gate, outcome, outcome, stamp),
        )
        db.execute(
            """INSERT INTO human_test_bindings VALUES
            (?,?,?,?,?,7,?,?,NULL,'Run',?)""",
            (
                gate,
                str(seed.project),
                str(seed.milestone),
                seed.review,
                seed.pr,
                sha,
                seed.ci,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO human_test_results VALUES (?,?,?,?,NULL,?,?,?)",
            (result, gate, response, outcome, sha, seed.ci, stamp),
        )


@pytest.mark.parametrize("state", ["CANCELLED", "EXPIRED"])
def test_cancelled_or_expired_human_test_blocks(db, state: str) -> None:
    seed, github = seed_eligible(db)
    add_human_test(db, seed, state=state)
    result = Gatekeeper(db, github).evaluate(seed.request, now=NOW)
    assert not guard(result, "human_gates") and not guard(result, "human_test")


@pytest.mark.parametrize("outcome", ["FAIL", "BLOCKED"])
def test_non_pass_human_test_blocks(db, outcome: str) -> None:
    seed, github = seed_eligible(db)
    add_human_test(db, seed, outcome=outcome)
    assert not guard(
        Gatekeeper(db, github).evaluate(seed.request, now=NOW), "human_test"
    )


def test_stale_human_test_blocks_and_current_pass_succeeds(db) -> None:
    seed, github = seed_eligible(db, ci_sha=OTHER_SHA)
    # Binding FK requires a matching historical CI record.
    add_human_test(db, seed, sha=OTHER_SHA)
    db.execute(
        """INSERT INTO ci_runs
        (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,overall_status,
         started_at,completed_at,last_checked_at,summary_json,retry_count)
        VALUES (?,?,?,?,?,2,'PASSED',?,?,?,'{}',0)""",
        (
            uid(),
            str(seed.project),
            str(seed.milestone),
            seed.pr,
            SHA,
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    result = Gatekeeper(db, github).evaluate(seed.request, now=NOW)
    assert not guard(result, "human_test")


def test_current_human_test_pass_succeeds_with_binding_evidence(db) -> None:
    seed, github = seed_eligible(db)
    add_human_test(db, seed)
    result = Gatekeeper(db, github).evaluate(seed.request, now=NOW)
    assert result.eligible
    assert result.evidence.human_test_result_id
    assert result.evidence.human_test_ci_run_id == seed.ci


def test_security_policy_blocks(db) -> None:
    seed, github = seed_eligible(db)
    result = Gatekeeper(db, github, security_blocked=lambda _p, _m: True).evaluate(
        seed.request, now=NOW
    )
    assert not guard(result, "security_policy")


def test_persisted_security_policy_blocks_evaluation(db) -> None:
    seed, github = seed_eligible(db)
    SecurityPolicy(db).record(
        SecurityEventType.SECRET_DETECTED,
        SecuritySeverity.HIGH,
        project_id=seed.project,
        milestone_id=seed.milestone,
        source_component="test_policy",
        correlation_id="before-evaluate",
        blocking=True,
        created_at=NOW,
    )
    result = Gatekeeper(db, github).evaluate(seed.request, now=NOW)
    assert not result.eligible
    assert not guard(result, "security_policy")


def test_mismatched_request_identity_is_rejected_and_audited_without_fk_error(
    db,
) -> None:
    seed, github = seed_eligible(db)
    wrong = replace(
        seed.request,
        project_id=ProjectId.generate(),
        repository_id=99,
        pull_request_number=99,
    )
    result = Gatekeeper(db, github).evaluate(wrong, now=NOW)
    assert not result.eligible
    assert db.execute(
        "SELECT 1 FROM merge_eligibility_results WHERE id=?", (result.result_id,)
    ).fetchone()


@pytest.mark.parametrize(
    "replacement",
    [
        {"project_id": ProjectId.generate()},
        {"milestone_id": MilestoneId.generate()},
        {"github_repository_id": "unrelated-repository"},
        {"pull_request_id": "unrelated-pull-request"},
    ],
)
def test_each_internal_identity_mismatch_remains_auditable(db, replacement) -> None:
    seed, github = seed_eligible(db)
    result = Gatekeeper(db, github).evaluate(
        replace(seed.request, **replacement), now=NOW
    )
    assert not result.eligible
    assert (
        db.execute(
            "SELECT eligible FROM merge_eligibility_results WHERE id=?",
            (result.result_id,),
        ).fetchone()[0]
        == 0
    )


def prepared(db) -> tuple[Seed, FakeGitHub, Gatekeeper, str, MergeRequest]:
    seed, github = seed_eligible(db)
    keeper = Gatekeeper(db, github)
    attempt, request = keeper.prepare(seed.request, now=NOW)
    return seed, github, keeper, attempt, request


def test_prepare_persists_before_mutation_and_global_serializes(db) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    assert (
        db.execute(
            "SELECT status FROM merge_attempts WHERE id=?", (attempt,)
        ).fetchone()[0]
        == "REQUESTED"
    )
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "MERGING"
    )
    assert github.merge_calls == []
    # Direct constraint is durable and independent of a Python lock.
    row = db.execute("SELECT * FROM merge_attempts WHERE id=?", (attempt,)).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO merge_attempts
            (id,project_id,milestone_id,pull_request_id,expected_head_sha,
             expected_head_branch,expected_base_branch,
             gatekeeper_result_id,gatekeeper_result_json,merge_strategy,status,requested_at)
             VALUES (?,?,?,?,?,'feature','main',?,?,?, 'REQUESTED',?)""",
            (
                uid(),
                row["project_id"],
                row["milestone_id"],
                row["pull_request_id"],
                row["expected_head_sha"],
                row["gatekeeper_result_id"],
                row["gatekeeper_result_json"],
                "SQUASH",
                NOW.isoformat(),
            ),
        )


@pytest.mark.parametrize(
    "live_change",
    [
        {"base_branch": "release"},
        {"head_branch": "other"},
        {"head_sha": OTHER_SHA},
        {"state": PullRequestState.CLOSED},
    ],
)
def test_fresh_deterministic_mismatch_terminalises_and_blocks(db, live_change) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    github.live = replace(github.live, **live_change)
    result = keeper.execute(attempt, request, now=NOW)
    assert result.status is MergeStatus.REJECTED
    assert github.merge_calls == []
    assert (
        db.execute(
            "SELECT status FROM merge_attempts WHERE id=?", (attempt,)
        ).fetchone()[0]
        == "REJECTED"
    )
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "BLOCKED"
    )


def test_terminal_pre_put_mismatch_releases_global_merge_slot(db) -> None:
    _, github, keeper, attempt, request = prepared(db)
    github.live = replace(github.live, base_branch="release")
    keeper.execute(attempt, request, now=NOW)
    row = db.execute("SELECT * FROM merge_attempts WHERE id=?", (attempt,)).fetchone()
    db.execute(
        """INSERT INTO merge_attempts
        (id,project_id,milestone_id,pull_request_id,expected_head_sha,
         expected_head_branch,expected_base_branch,gatekeeper_result_id,
         gatekeeper_result_json,merge_strategy,status,requested_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,'REQUESTED',?)""",
        (
            uid(),
            row["project_id"],
            row["milestone_id"],
            row["pull_request_id"],
            row["expected_head_sha"],
            row["expected_head_branch"],
            row["expected_base_branch"],
            row["gatekeeper_result_id"],
            row["gatekeeper_result_json"],
            row["merge_strategy"],
            NOW.isoformat(),
        ),
    )


@pytest.mark.parametrize("state", ["PAUSED", "CANCELLED", "BLOCKED"])
def test_project_policy_change_after_prepare_prevents_put(db, state: str) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    db.execute("UPDATE projects SET state=? WHERE id=?", (state, str(seed.project)))
    result = keeper.execute(attempt, request, now=NOW)
    assert result.status is MergeStatus.REJECTED
    assert github.merge_calls == []
    assert (
        db.execute(
            "SELECT status FROM merge_attempts WHERE id=?", (attempt,)
        ).fetchone()[0]
        == "REJECTED"
    )


def test_security_block_activated_after_prepare_prevents_put(db) -> None:
    seed, github = seed_eligible(db)
    blocked = False
    keeper = Gatekeeper(db, github, security_blocked=lambda _p, _m: blocked)
    attempt, request = keeper.prepare(seed.request, now=NOW)
    blocked = True
    assert keeper.execute(attempt, request, now=NOW).status is MergeStatus.REJECTED
    assert github.merge_calls == []


def test_persisted_security_event_after_prepare_prevents_put(db) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    SecurityPolicy(db).record(
        SecurityEventType.REPOSITORY_IDENTITY_MISMATCH,
        SecuritySeverity.CRITICAL,
        project_id=seed.project,
        milestone_id=seed.milestone,
        source_component="test_policy",
        correlation_id="after-prepare",
        blocking=True,
        created_at=NOW,
    )
    assert keeper.execute(attempt, request, now=NOW).status is MergeStatus.REJECTED
    assert github.merge_calls == []


def test_new_unresolved_gate_after_prepare_prevents_put(db) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    add_decision_gate(db, seed, "PRODUCT_DECISION", "PENDING")
    assert keeper.execute(attempt, request, now=NOW).status is MergeStatus.REJECTED
    assert github.merge_calls == []


def test_new_blocking_finding_after_prepare_prevents_put(db) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    db.execute(
        """INSERT INTO architect_review_findings VALUES
        (?,?,'late-block','major','M26','late','fix','OPEN',NULL,?)""",
        (uid(), seed.review, NOW.isoformat()),
    )
    assert keeper.execute(attempt, request, now=NOW).status is MergeStatus.REJECTED
    assert github.merge_calls == []


@pytest.mark.parametrize(
    ("column", "value"), [("state", "CLOSED"), ("head_sha", OTHER_SHA)]
)
def test_persisted_pr_freshness_change_after_prepare_prevents_put(
    db, column: str, value: str
) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    prior_gets = len(github.get_calls)
    db.execute(f"UPDATE pull_requests SET {column}=? WHERE id=?", (value, seed.pr))
    assert keeper.execute(attempt, request, now=NOW).status is MergeStatus.REJECTED
    assert len(github.get_calls) == prior_gets
    assert github.merge_calls == []


def test_transient_pre_put_get_failure_remains_retryable(db) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    github.get_queue = [TimeoutError("temporary")]
    with pytest.raises(TimeoutError):
        keeper.execute(attempt, request, now=NOW)
    assert github.merge_calls == []
    assert (
        db.execute(
            "SELECT status FROM merge_attempts WHERE id=?", (attempt,)
        ).fetchone()[0]
        == "REQUESTED"
    )
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "MERGING"
    )


def test_already_merged_pre_put_is_reconciled_without_put_then_verified(db) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    github.live = replace(
        github.live,
        state=PullRequestState.MERGED,
        merged_at=NOW.isoformat(),
        merge_commit_sha=MERGE_SHA,
    )
    result = keeper.execute(attempt, request, now=NOW)
    assert result.status is MergeStatus.MERGED
    assert github.merge_calls == []
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "MERGE_VERIFY"
    )
    assert keeper.verify(attempt, request, now=NOW)
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "COMPLETE"
    )


def test_unrelated_repository_argument_cannot_select_mutation_target(db) -> None:
    _, github, keeper, attempt, request = prepared(db)
    result = keeper.execute(attempt, request, now=NOW)
    assert result.status is MergeStatus.MERGED
    assert github.merge_calls[0][0] == "owner/repo"
    assert github.merge_calls[0][1].expected_head_sha == SHA


def test_cross_attempt_request_cannot_execute_or_verify(db) -> None:
    _, github, keeper, attempt, request = prepared(db)
    wrong = replace(request, project_id=ProjectId.generate())
    with pytest.raises(MergeIdentityError):
        keeper.execute(attempt, wrong, now=NOW)
    assert github.merge_calls == []
    keeper.execute(attempt, request, now=NOW)
    with pytest.raises(MergeIdentityError):
        keeper.verify(attempt, wrong, now=NOW)


@pytest.mark.parametrize(
    "replacement",
    [
        {"milestone_id": MilestoneId.generate()},
        {"repository_id": 99},
        {"pull_request_number": 99},
        {"expected_head_sha": OTHER_SHA},
        {"gatekeeper_result_id": "other-result"},
        {"merge_strategy": MergeStrategy.REBASE},
    ],
)
def test_all_durable_attempt_identities_are_bound(db, replacement) -> None:
    _, github, keeper, attempt, request = prepared(db)
    with pytest.raises(MergeIdentityError):
        keeper.execute(attempt, replace(request, **replacement), now=NOW)
    assert github.merge_calls == []


@pytest.mark.parametrize(
    "status", [MergeStatus.CONFLICT, MergeStatus.REJECTED, MergeStatus.NOT_MERGED]
)
def test_known_non_merge_outcomes_block_milestone(db, status: MergeStatus) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    github.merge_result = MergeResult(
        MERGE_INTERFACE_VERSION, seed.project, seed.milestone, 7, status, detail="known"
    )
    assert keeper.execute(attempt, request, now=NOW).status is status
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "BLOCKED"
    )
    assert (
        db.execute(
            "SELECT status FROM merge_attempts WHERE id=?", (attempt,)
        ).fetchone()[0]
        == status.value
    )


def test_ambiguous_merge_observes_once_without_second_put(db) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    merged = replace(
        github.live,
        state=PullRequestState.MERGED,
        merged_at=NOW.isoformat(),
        merge_commit_sha=MERGE_SHA,
    )
    github.merge_result = AmbiguousGitHubResult("lost")
    github.get_queue = [github.live, merged]
    result = keeper.execute(attempt, request, now=NOW)
    assert result.status is MergeStatus.MERGED
    assert len(github.merge_calls) == 1
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "MERGE_VERIFY"
    )


def test_unresolved_ambiguity_is_unknown_and_never_replayed(db) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    github.merge_result = AmbiguousGitHubResult("lost")
    result = keeper.execute(attempt, request, now=NOW)
    assert result.status is MergeStatus.UNKNOWN
    assert len(github.merge_calls) == 1
    assert (
        db.execute(
            "SELECT status FROM merge_attempts WHERE id=?", (attempt,)
        ).fetchone()[0]
        == "UNKNOWN"
    )
    with pytest.raises(MergeIdentityError):
        keeper.execute(attempt, request, now=NOW)
    assert len(github.merge_calls) == 1
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "MERGING"
    )


def test_provider_success_only_reaches_verify_then_independent_get_completes(
    db,
) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    called = False

    def assert_intent() -> None:
        nonlocal called
        called = True
        row = db.execute(
            "SELECT status,mutation_started_at FROM merge_attempts WHERE id=?",
            (attempt,),
        ).fetchone()
        assert tuple(row) == ("REQUESTED", NOW.isoformat())

    github.before_merge = assert_intent
    keeper.execute(attempt, request, now=NOW)
    assert called
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "MERGE_VERIFY"
    )
    assert (
        db.execute("SELECT state FROM pull_requests WHERE id=?", (seed.pr,)).fetchone()[
            0
        ]
        == "OPEN"
    )
    assert not keeper.verify(attempt, request, now=NOW)
    github.live = replace(
        github.live,
        state=PullRequestState.MERGED,
        merged_at=NOW.isoformat(),
        merge_commit_sha=MERGE_SHA,
    )
    assert keeper.verify(attempt, request, now=NOW)
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "COMPLETE"
    )
    row = db.execute(
        "SELECT status,merge_commit_sha FROM merge_attempts WHERE id=?", (attempt,)
    ).fetchone()
    assert tuple(row) == ("MERGED", MERGE_SHA)
    assert (
        db.execute("SELECT state FROM pull_requests WHERE id=?", (seed.pr,)).fetchone()[
            0
        ]
        == "MERGED"
    )


def test_wrong_verification_identity_cannot_complete(db) -> None:
    seed, github, keeper, attempt, request = prepared(db)
    keeper.execute(attempt, request, now=NOW)
    github.live = replace(
        github.live,
        state=PullRequestState.MERGED,
        base_branch="wrong",
        merged_at=NOW.isoformat(),
        merge_commit_sha=MERGE_SHA,
    )
    assert not keeper.verify(attempt, request, now=NOW)
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
        ).fetchone()[0]
        == "MERGE_VERIFY"
    )


def test_duplicate_execution_never_repeats_put(db) -> None:
    _, github, keeper, attempt, request = prepared(db)
    keeper.execute(attempt, request, now=NOW)
    with pytest.raises(MergeIdentityError):
        keeper.execute(attempt, request, now=NOW)
    assert len(github.merge_calls) == 1


def test_m26_smoke_reports_attempt_bound_gatekeeper_result(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "smoke.db"
    with open_database(path) as connection:
        apply_migrations(connection)
        seed, github = seed_eligible(connection)

    def observe_merged() -> None:
        github.live = replace(
            github.live,
            state=PullRequestState.MERGED,
            merged_at=NOW.isoformat(),
            merge_commit_sha=MERGE_SHA,
        )

    github.before_merge = observe_merged
    monkeypatch.setattr(m26_smoke, "load_m18_host_config", lambda: object())
    monkeypatch.setattr(m26_smoke, "GitHubPullRequestAdapter", lambda _config: github)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "m26_smoke",
            "--database",
            str(path),
            "--project-id",
            str(seed.project),
            "--milestone-id",
            str(seed.milestone),
            "--pull-request-id",
            seed.pr,
            "--correlation-id",
            "smoke-correlation",
        ],
    )

    assert m26_smoke.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["eligible"] is True
    assert output["verified_complete"] is True
    assert output["gatekeeper_result_id"] != output["preflight_gatekeeper_result_id"]

    with open_database(path) as connection:
        attempt = connection.execute(
            "SELECT * FROM merge_attempts WHERE id=?", (output["merge_attempt_id"],)
        ).fetchone()
        transition = connection.execute(
            """SELECT metadata_json FROM state_transitions
            WHERE milestone_id=? AND new_state='MERGING' ORDER BY rowid DESC LIMIT 1""",
            (str(seed.milestone),),
        ).fetchone()
    assert attempt["gatekeeper_result_id"] == output["gatekeeper_result_id"]
    assert (
        json.loads(transition["metadata_json"])["gatekeeper_result_id"]
        == output["gatekeeper_result_id"]
    )
