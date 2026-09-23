"""Explicit host-acceptance entry point for an existing disposable M19 workspace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from syntra_build.application.codex import BoundCodexRunner
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain.codex import CodexRunRequest
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.infrastructure.codex_runner import LocalCodexCliRunner
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence.connection import open_database


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an M20 smoke against an existing disposable M19 worktree"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--milestone-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--other-worktree", type=Path, required=True)
    parser.add_argument("--secret-path", type=Path, required=True)
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--agents-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    project_id = ProjectId.from_string(args.project_id)
    milestone_id = MilestoneId.from_string(args.milestone_id)
    job_id = JobId.from_string(args.job_id)
    with open_database(args.database) as connection:
        provider = LocalCodexCliRunner(
            connection,
            executable=args.executable,
            worker_identity="syntra-codex",
            artifact_root=args.artifact_root,
        )
        service = WorkspaceService(
            connection, TrustedGit(args.data_root / "git-auth"), args.data_root
        )
        assigned = args.worktree.resolve(strict=True)
        other = args.other_worktree.resolve(strict=True)
        workspace_root = (args.data_root / "workspaces").resolve(strict=True)
        if (
            assigned == other
            or workspace_root not in other.parents
            or args.secret_path.resolve(strict=True)
            == args.state_path.resolve(strict=True)
        ):
            parser.error("host-smoke denial targets are invalid")
        before = service.inspect(project_id, milestone_id)
        runner = BoundCodexRunner(service, provider)
        result = runner.run(
            CodexRunRequest(
                "1.0",
                f"m20-smoke-{job_id}",
                project_id,
                milestone_id,
                job_id,
                args.attempt,
                args.worktree,
                {
                    "objective": "M20 disposable identity and isolation smoke",
                    "task": (
                        "Report id -un, HOME, pwd, and environment variable names; "
                        "create .syntra-m20-smoke containing ok; prove these paths "
                        "cannot be read or written: "
                        f"other workspace={other}, secret={args.secret_path}, "
                        f"state={args.state_path}. Do not alter Git HEAD."
                    ),
                },
                args.agents_file.read_text(encoding="utf-8"),
                args.timeout,
            )
        )
        after = service.inspect(project_id, milestone_id)
    print(
        json.dumps(
            {
                "status": result.process_status,
                "worker_identity": result.worker_identity,
                "process_id": result.process_id,
                "exit_code": result.exit_code,
                "stdout_reference": result.stdout_reference,
                "stderr_reference": result.stderr_reference,
                "summary": result.summary,
                "head_unchanged": before.head_sha == after.head_sha,
                "filesystem_changes_present": not after.clean,
            },
            sort_keys=True,
        )
    )
    return 0 if result.process_status.value == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
