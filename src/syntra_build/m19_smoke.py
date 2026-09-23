"""Explicit, non-destructive M19 host acceptance entry point."""

from __future__ import annotations

import json
import os

from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain import MilestoneId, ProjectId
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import bootstrap_database
from syntra_build.m18_smoke import load_m18_host_config


def main() -> None:
    """Prepare and inspect exactly the operator-supplied project and milestone."""
    raw_project = os.environ.get("SYNTRA_M19_PROJECT_ID")
    raw_milestone = os.environ.get("SYNTRA_M19_MILESTONE_ID")
    if not raw_project or not raw_milestone:
        raise SystemExit(
            "SYNTRA_M19_PROJECT_ID and SYNTRA_M19_MILESTONE_ID are required"
        )
    config = load_m18_host_config()
    assert config.github.owner is not None
    assert config.secrets.github_token is not None
    connection = bootstrap_database(config)
    try:
        service = WorkspaceService(
            connection,
            TrustedGit(
                config.filesystem.data_root / "authentication",
                username=config.github.owner,
                token=config.secrets.github_token,
            ),
            config.filesystem.data_root,
        )
        project_id, milestone_id = (
            ProjectId.from_string(raw_project),
            MilestoneId.from_string(raw_milestone),
        )
        workspace = service.prepare_workspace(project_id, milestone_id)
        inspection = service.inspect(project_id, milestone_id)
        repository = service.records.managed_for_project(project_id)
        assert repository is not None
        print(
            json.dumps(
                {
                    "project_id": raw_project,
                    "milestone_id": raw_milestone,
                    "repository_path": str(repository.path),
                    "remote_main_sha": repository.last_known_main_sha,
                    "branch": workspace.branch_name,
                    "worktree_path": str(workspace.path),
                    "head_sha": inspection.head_sha,
                    "state": inspection.workspace.state.value,
                    "clean": inspection.clean,
                    "registered": inspection.registered,
                },
                sort_keys=True,
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
