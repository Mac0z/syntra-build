"""Host-acceptance seam for an existing disposable M19/M20 workspace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from syntra_build.application.change_validation import ChangeValidationService
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.workspaces import WorkspaceError
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence.connection import open_database


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate or commit an existing disposable M21 worktree"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--milestone-id", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument("--authorised-protected-path", action="append", default=[])
    parser.add_argument("--expected-diff-hash")
    parser.add_argument("--commit-message", default="M21 host smoke")
    parser.add_argument("--commit-path", action="append", default=[])
    args = parser.parse_args()
    project_id = ProjectId.from_string(args.project_id)
    milestone_id = MilestoneId.from_string(args.milestone_id)
    git = TrustedGit(args.data_root / "git-auth")
    with open_database(args.database) as connection:
        if args.expected_diff_hash:
            try:
                workspace = WorkspaceService(connection, git, args.data_root)
                inspection = workspace.inspect(project_id, milestone_id)
                commit = workspace.commit(
                    project_id,
                    milestone_id,
                    inspection.head_sha,
                    args.commit_path,
                    args.commit_message,
                    expected_diff_hash=args.expected_diff_hash,
                )
            except WorkspaceError as error:
                print(json.dumps({"committed": False, "reason": str(error)}))
                return 2
            print(json.dumps({"committed": True, "commit_sha": commit.commit_sha}))
            return 0
        result = ChangeValidationService(connection, git, args.data_root).validate(
            project_id,
            milestone_id,
            args.correlation_id,
            authorised_protected_paths=args.authorised_protected_path,
        )
    print(
        json.dumps(
            {
                "change_set_id": result.id,
                "decision": result.decision,
                "diff_hash": result.diff_hash,
                "is_empty": result.is_empty,
                "findings": [
                    {
                        "code": item.code,
                        "severity": item.severity,
                        "path": item.path,
                        "location": item.location,
                        "fingerprint": item.fingerprint,
                        "message": item.message,
                        "blocking": item.blocking,
                    }
                    for item in result.findings
                ],
                "workspace_preserved": True,
                "push_performed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if result.decision.value == "ACCEPT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
