"""Bounded, externally read-only M23 CI reconciliation smoke seam."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from syntra_build.adapters.github.actions import GitHubActionsAdapter
from syntra_build.adapters.github.pull_requests import GitHubPullRequestAdapter
from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.m18_smoke import load_m18_host_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile CI for one trusted M22 PR")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--milestone-id", required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args()
    config = load_m18_host_config()
    project_id = ProjectId.from_string(args.project_id)
    milestone_id = MilestoneId.from_string(args.milestone_id)
    with open_database(args.database) as connection:
        monitor = CIMonitor(
            connection, GitHubPullRequestAdapter(config), GitHubActionsAdapter(config)
        )
        run = monitor.reconcile(project_id, milestone_id, args.correlation_id)
        if run.head_sha != args.expected_head_sha:
            raise RuntimeError("live PR head differs from --expected-head-sha")
        pr = monitor.prs.for_milestone(milestone_id)
        assert pr is not None
    print(
        json.dumps(
            {
                "pull_request_number": pr.external_pr_number,
                "head_sha": run.head_sha,
                "overall_status": run.overall_status,
                "required_checks": [
                    {
                        "name": item.name,
                        "status": item.status,
                        "conclusion": item.conclusion,
                    }
                    for item in run.checks
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
