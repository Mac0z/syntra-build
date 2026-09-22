"""Explicit live M18 acceptance entry point for a disposable approved project."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from syntra_build.adapters.github.provisioning import GitHubProvisioningAdapter
from syntra_build.application.provisioning import RepositoryProvisioningService
from syntra_build.domain import ProjectId
from syntra_build.infrastructure.config import (
    DEFAULT_ARCHITECT_API_KEY_PATH,
    DEFAULT_GITHUB_TOKEN_PATH,
    DEFAULT_HOST_CONFIG_PATH,
    DEFAULT_TELEGRAM_TOKEN_PATH,
    ApplicationConfig,
    load_host_config,
)
from syntra_build.infrastructure.git_initial import SubprocessInitialBaselineGit
from syntra_build.infrastructure.persistence import bootstrap_database


def load_m18_host_config(
    config_path: Path = DEFAULT_HOST_CONFIG_PATH,
    *,
    github_token_path: Path = DEFAULT_GITHUB_TOKEN_PATH,
    telegram_token_path: Path = DEFAULT_TELEGRAM_TOKEN_PATH,
    architect_api_key_path: Path = DEFAULT_ARCHITECT_API_KEY_PATH,
) -> ApplicationConfig:
    """Load established host files and require the M18 GitHub integration."""
    config = load_host_config(
        config_path,
        github_token_path=github_token_path,
        telegram_token_path=telegram_token_path,
        architect_api_key_path=architect_api_key_path,
    )
    if (
        not config.github.enabled
        or config.github.owner is None
        or config.secrets.github_token is None
    ):
        raise RuntimeError("GitHub provisioning configuration is required")
    return config


def main() -> None:
    """Provision one existing approved project named by SYNTRA_M18_PROJECT_ID."""
    raw_project_id = os.environ.get("SYNTRA_M18_PROJECT_ID")
    if not raw_project_id:
        raise SystemExit("SYNTRA_M18_PROJECT_ID is required")
    config = load_m18_host_config()
    token = config.secrets.github_token
    assert token is not None
    assert config.github.owner is not None
    connection = bootstrap_database(config)
    try:
        service = RepositoryProvisioningService(
            connection,
            GitHubProvisioningAdapter(config),
            SubprocessInitialBaselineGit(
                config.filesystem.data_root / "m18-provisioning",
                github_username=config.github.owner,
                github_token=token,
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
