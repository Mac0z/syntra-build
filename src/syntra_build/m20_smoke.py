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
                    "task": "Create or replace .syntra-m20-smoke with the text ok.",
                },
                args.agents_file.read_text(encoding="utf-8"),
                args.timeout,
            )
        )
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
            },
            sort_keys=True,
        )
    )
    return 0 if result.process_status.value == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
