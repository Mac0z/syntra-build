"""Explicit live M22 acceptance seam for an existing trusted milestone commit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from syntra_build.adapters.github.pull_requests import GitHubPullRequestAdapter
from syntra_build.application.pull_requests import PullRequestLifecycleService
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.infrastructure.config import ApplicationConfig
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.m18_smoke import load_m18_host_config


def trusted_git_from_host_config(
    config: ApplicationConfig, supplied_data_root: Path
) -> TrustedGit:
    """Build the authenticated Git seam only for the configured workspace root."""
    configured_root = config.filesystem.data_root.resolve()
    if supplied_data_root.resolve() != configured_root:
        raise ValueError("--data-root must match the configured filesystem data root")
    assert config.github.owner is not None
    assert config.secrets.github_token is not None
    return TrustedGit(
        configured_root / "authentication",
        username=config.github.owner,
        token=config.secrets.github_token,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile one trusted M22 implementation PR"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--milestone-id", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--change-set-id", required=True)
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args()
    config = load_m18_host_config()
    trusted_git = trusted_git_from_host_config(config, args.data_root)
    data_root = config.filesystem.data_root
    with open_database(args.database) as connection:
        result = PullRequestLifecycleService(
            connection,
            WorkspaceService(connection, trusted_git, data_root),
            GitHubPullRequestAdapter(config),
        ).establish_for_commit(
            ProjectId.from_string(args.project_id),
            MilestoneId.from_string(args.milestone_id),
            args.commit_sha,
            args.change_set_id,
            args.correlation_id,
        )
    print(
        json.dumps(
            {
                "pull_request_id": result.id,
                "pull_request_number": result.external_pr_number,
                "state": result.state,
                "head_sha": result.head_sha,
                "web_url": result.web_url,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
