"""Explicit live M18 acceptance entry point for a disposable approved project."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from syntra_build.adapters.github.provisioning import GitHubProvisioningAdapter
from syntra_build.application.provisioning import RepositoryProvisioningService
from syntra_build.domain import ProjectId
from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.git_initial import SubprocessInitialBaselineGit
from syntra_build.infrastructure.persistence import bootstrap_database


def main() -> None:
    """Provision one existing approved project named by SYNTRA_M18_PROJECT_ID."""
    raw_project_id = os.environ.get("SYNTRA_M18_PROJECT_ID")
    if not raw_project_id:
        raise SystemExit("SYNTRA_M18_PROJECT_ID is required")
    config = load_config()
    token = config.secrets.github_token
    if token is None or config.github.owner is None:
        raise SystemExit("GitHub provisioning configuration is required")
    # Ephemeral process environment passes authentication without placing it in
    # the remote URL or .git/config. Neither command output nor this report includes it.
    push_environment = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token.value}",
    }
    connection = bootstrap_database(config)
    try:
        service = RepositoryProvisioningService(
            connection,
            GitHubProvisioningAdapter(config),
            SubprocessInitialBaselineGit(
                config.filesystem.data_root / "m18-provisioning",
                push_environment=push_environment,
            ),
            owner=config.github.owner,
        )
        result = service.provision(
            ProjectId.from_string(raw_project_id),
            datetime.now(UTC),
            f"m18-smoke:{raw_project_id}",
        )
        print(
            json.dumps(
                {
                    "project_id": str(result.project_id),
                    "project_name": result.project_name,
                    "repository_full_name": result.repository_full_name,
                    "visibility": result.visibility.value,
                    "github_external_repository_id": result.external_repository_id,
                    "baseline_commit_sha": result.baseline_commit_sha,
                    "approved_spec_hash": result.spec_hash,
                    "approved_agents_hash": result.agents_hash,
                    "final_verification": result.verified,
                    "project_state": result.project_state.value,
                },
                sort_keys=True,
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
