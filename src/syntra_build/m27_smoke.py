"""Bounded host acceptance entry point for persisted M27 crash fixtures."""

import argparse
from pathlib import Path

from syntra_build.adapters.github.actions import GitHubActionsAdapter
from syntra_build.adapters.github.pull_requests import GitHubPullRequestAdapter
from syntra_build.application.ci_handoff import PullRequestCIHandoff
from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.application.gatekeeper import Gatekeeper
from syntra_build.application.human_intervention import HumanInterventionService
from syntra_build.application.pull_requests import PullRequestLifecycleService
from syntra_build.application.recovery import RecoveryCoordinator, RecoveryServices
from syntra_build.application.scheduler import Scheduler, WorkerCapacity
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.infrastructure.config import SchedulerConfig
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    apply_migrations,
    open_database,
)
from syntra_build.m18_smoke import load_m18_host_config
from syntra_build.m22_smoke import trusted_git_from_host_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile a controlled M27 database")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("github", "local", "human", "all"), required=True
    )
    parser.add_argument("--data-root", type=Path)
    args = parser.parse_args()
    with open_database(args.database) as connection:
        apply_migrations(connection)
        scheduler = Scheduler(
            SQLiteJobRepository(connection, lambda: "smoke-job-history"),
            WorkerCapacity(SchedulerConfig().worker_class_limits()),
            {},
        )
        try:
            services = RecoveryServices()
            workspace = None
            if args.mode in {"github", "local", "all"}:
                if args.data_root is None:
                    parser.error("--data-root is required for github/local/all modes")
                config = load_m18_host_config()
                workspace = WorkspaceService(
                    connection,
                    trusted_git_from_host_config(config, args.data_root),
                    config.filesystem.data_root,
                )
                services = RecoveryServices(workspace=workspace)
                if args.mode in {"github", "all"}:
                    prs = GitHubPullRequestAdapter(config)
                    lifecycle = PullRequestLifecycleService(connection, workspace, prs)
                    services = RecoveryServices(
                        pull_requests=PullRequestCIHandoff(connection, lifecycle),
                        ci=CIMonitor(connection, prs, GitHubActionsAdapter(config)),
                        gatekeeper=Gatekeeper(connection, prs),
                        workspace=workspace,
                    )
            if args.mode in {"human", "all"}:
                services = RecoveryServices(
                    pull_requests=services.pull_requests,
                    ci=services.ci,
                    gatekeeper=services.gatekeeper,
                    workspace=services.workspace,
                    human=HumanInterventionService(
                        connection, authorised_responder_ids=frozenset()
                    ),
                )

            def clean(subject):  # type: ignore[no-untyped-def]
                if workspace is None or subject.milestone_id is None:
                    return False
                return workspace.inspect(subject.project_id, subject.milestone_id).clean

            coordinator = RecoveryCoordinator(
                connection, scheduler, services=services, workspace_clean=clean
            )
            run_id = coordinator.recover()
            for row in coordinator.repository.observations(run_id):
                print(
                    f"{row['project_id']} {row['category']} "
                    f"{row['disposition']} {row['resulting_action']}"
                )
        finally:
            scheduler.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
