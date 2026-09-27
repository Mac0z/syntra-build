"""Bounded host acceptance seam for one fully-evidenced M26 merge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from syntra_build.adapters.github.pull_requests import GitHubPullRequestAdapter
from syntra_build.application.gatekeeper import Gatekeeper
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.merges import (
    MERGE_INTERFACE_VERSION,
    MergeEligibilityRequest,
    MergeStatus,
    MergeStrategy,
)
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.m18_smoke import load_m18_host_config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate, persist, execute, and independently verify one M26 merge"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--milestone-id", required=True)
    parser.add_argument("--pull-request-id", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument(
        "--strategy",
        choices=[item.value for item in MergeStrategy],
        default=MergeStrategy.SQUASH.value,
    )
    args = parser.parse_args()
    config = load_m18_host_config()
    github = GitHubPullRequestAdapter(config)
    project_id = ProjectId.from_string(args.project_id)
    milestone_id = MilestoneId.from_string(args.milestone_id)
    with open_database(args.database) as connection:
        row = connection.execute(
            """SELECT p.id AS pull_request_id,p.external_pr_number,
            p.head_branch,p.base_branch,p.head_sha,p.github_repository_id,
            r.external_repository_id FROM pull_requests p
            JOIN github_repositories r ON r.id=p.github_repository_id
            WHERE p.id=? AND p.project_id=? AND p.milestone_id=?
              AND r.status='VERIFIED'""",
            (args.pull_request_id, args.project_id, args.milestone_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "exact persisted PR and verified repository are required"
            )
        request = MergeEligibilityRequest(
            MERGE_INTERFACE_VERSION,
            args.correlation_id,
            project_id,
            milestone_id,
            row["github_repository_id"],
            row["external_repository_id"],
            row["pull_request_id"],
            row["external_pr_number"],
            row["head_branch"],
            row["base_branch"],
            row["head_sha"],
        )
        gatekeeper = Gatekeeper(connection, github)
        eligibility = gatekeeper.evaluate(request)
        if not eligibility.eligible:
            output = {
                "eligible": False,
                "gatekeeper_result_id": eligibility.result_id,
                "failed_guards": [
                    item.guard for item in eligibility.guards if not item.passed
                ],
            }
        else:
            attempt_id, merge_request = gatekeeper.prepare(
                request, MergeStrategy(args.strategy)
            )
            result = gatekeeper.execute(attempt_id, merge_request)
            verified = (
                gatekeeper.verify(attempt_id, merge_request)
                if result.status is MergeStatus.MERGED
                else False
            )
            output = {
                "eligible": True,
                "gatekeeper_result_id": eligibility.result_id,
                "merge_attempt_id": attempt_id,
                "merge_status": result.status.value,
                "verified_complete": verified,
            }
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
