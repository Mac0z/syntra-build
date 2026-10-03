"""Reviewed production composition for the released M32 scheduler routes."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from uuid import uuid4

from syntra_build.adapters.architect import OpenAIArchitectProvider
from syntra_build.adapters.github.actions import GitHubActionsAdapter
from syntra_build.adapters.github.provisioning import GitHubProvisioningAdapter
from syntra_build.adapters.github.pull_requests import GitHubPullRequestAdapter
from syntra_build.application.architect import ArchitectDesignService
from syntra_build.application.architect_review import ArchitectReviewService
from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.application.ci_scheduler import (
    CIJobCoordinator,
    CIReconciliationExecutor,
)
from syntra_build.application.codex import BoundCodexRunner
from syntra_build.application.design import ProjectDesignContextService
from syntra_build.application.gatekeeper import Gatekeeper
from syntra_build.application.human_intervention import HumanInterventionService
from syntra_build.application.lifecycle import JobTypeDispatcher, LifecycleCoordinator
from syntra_build.application.m32_executors import (
    ArchitectDesignExecutor,
    ArchitectReviewExecutor,
    ArchitectTaskExecutor,
    ChangeValidationExecutor,
    GatekeeperMergeExecutor,
    InitialCodexExecutor,
    PullRequestCreateExecutor,
    RepositoryProvisioningExecutor,
    SpecificationDraftExecutor,
    TrustedCommitExecutor,
    TrustedPushExecutor,
    WorkspacePrepareExecutor,
)
from syntra_build.application.provisioning import RepositoryProvisioningService
from syntra_build.application.pull_requests import PullRequestLifecycleService
from syntra_build.application.review_rework import ReviewReworkCodexExecutor
from syntra_build.application.security import SecurityPolicy
from syntra_build.application.specification import SpecificationDraftService
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain.jobs import WorkerClass
from syntra_build.infrastructure.codex_runner import LocalCodexCliRunner
from syntra_build.infrastructure.config.models import (
    ApplicationConfig,
    ConfigurationError,
)
from syntra_build.infrastructure.git_initial import SubprocessInitialBaselineGit
from syntra_build.infrastructure.metrics import PrometheusRecorder
from syntra_build.infrastructure.persistence import (
    SQLiteArchitectInteractionRepository,
    SQLiteDesignMessageRepository,
    SQLiteDesignPackageRepository,
    SQLiteHumanGateRepository,
    SQLiteProjectDecisionRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
)
from syntra_build.m22_smoke import trusted_git_from_host_config

PRODUCTION_JOB_TYPES: Mapping[WorkerClass, frozenset[str]] = {
    WorkerClass.ARCHITECT: frozenset(
        {
            "ARCHITECT_DESIGN",
            "SPECIFICATION_DRAFT",
            "ARCHITECT_TASK",
            "ARCHITECT_REVIEW",
        }
    ),
    WorkerClass.CODEX: frozenset({"CODEX_RUN", "CODEX_REVIEW_REWORK"}),
    WorkerClass.GIT: frozenset(
        {"WORKSPACE_PREPARE", "CHANGE_VALIDATE", "GIT_COMMIT", "GIT_PUSH"}
    ),
    WorkerClass.GITHUB: frozenset({"REPOSITORY_PROVISION", "PR_CREATE", "PR_MERGE"}),
    WorkerClass.CI: frozenset({"CI_RECONCILE"}),
}


def _context(connection: sqlite3.Connection) -> ProjectDesignContextService:
    projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
    return ProjectDesignContextService(
        projects,
        SQLiteDesignMessageRepository(connection),
        SQLiteProjectDecisionRepository(connection),
        SQLiteProjectDocumentRepository(connection),
    )


def build_production_executors(
    config: ApplicationConfig, *, metrics: PrometheusRecorder | None = None
) -> Mapping[WorkerClass, JobTypeDispatcher]:
    """Construct the exact closed M32 vocabulary without opening worker databases."""
    if (
        not config.architect.enabled
        or config.architect.provider != "openai"
        or not config.architect.model
        or config.secrets.architect_api_key is None
    ):
        raise ConfigurationError(
            "the configured OpenAI Architect integration is required"
        )
    if (
        not config.github.enabled
        or not config.github.owner
        or config.secrets.github_token is None
    ):
        raise ConfigurationError("the configured GitHub integration is required")
    if (
        not config.codex.executable.strip()
        or shutil.which(config.codex.executable) is None
    ):
        raise ConfigurationError("the configured Codex executable is required")

    database = config.database.sqlite_path
    data_root = config.filesystem.data_root
    recorder = metrics or PrometheusRecorder()
    trusted_git = trusted_git_from_host_config(config, data_root)
    github_prs = GitHubPullRequestAdapter(config)

    def architect_provider() -> OpenAIArchitectProvider:
        key = config.secrets.architect_api_key
        assert key is not None and config.architect.model is not None
        return OpenAIArchitectProvider(
            api_key=key.value,
            model=config.architect.model,
            reasoning_effort=config.architect.reasoning_effort,
            timeout_seconds=config.architect.api_timeout_seconds,
            metrics=recorder,
        )

    def design(connection: sqlite3.Connection) -> ArchitectDesignService:
        return ArchitectDesignService(
            _context(connection),
            SQLiteArchitectInteractionRepository(connection),
            architect_provider(),
            reasoning_effort=config.architect.reasoning_effort,
        )

    def specification(connection: sqlite3.Connection) -> SpecificationDraftService:
        projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
        documents = SQLiteProjectDocumentRepository(connection)
        return SpecificationDraftService(
            _context(connection),
            SQLiteArchitectInteractionRepository(connection),
            architect_provider(),
            SQLiteDesignPackageRepository(connection),
            documents,
            SQLiteHumanGateRepository(connection, lambda: str(uuid4())),
            projects,
            reasoning_effort=config.architect.reasoning_effort,
        )

    def workspace(connection: sqlite3.Connection) -> WorkspaceService:
        return WorkspaceService(connection, trusted_git, data_root)

    def runner(connection: sqlite3.Connection) -> BoundCodexRunner:
        process = LocalCodexCliRunner(
            connection,
            executable=config.codex.executable,
            worker_identity=config.codex.worker_identity,
            artifact_root=data_root / "codex-runs",
        )
        return BoundCodexRunner(workspace(connection), process)

    authorised = frozenset(str(item) for item in config.telegram.authorised_user_ids)

    def review(connection: sqlite3.Connection) -> ArchitectReviewService:
        return ArchitectReviewService(
            connection,
            github_prs,
            github_prs,
            architect_provider(),
            architect_rework_limit=config.retries.architect_rework_limit,
            reasoning_effort=config.architect.reasoning_effort,
            human_interventions=HumanInterventionService(
                connection, authorised_responder_ids=authorised
            ),
        )

    architect = JobTypeDispatcher(
        {
            "ARCHITECT_DESIGN": ArchitectDesignExecutor(database, design).execute,
            "SPECIFICATION_DRAFT": SpecificationDraftExecutor(
                database, specification
            ).execute,
            "ARCHITECT_TASK": ArchitectTaskExecutor(
                database, architect_provider()
            ).execute,
            "ARCHITECT_REVIEW": ArchitectReviewExecutor(database, review).execute,
        }
    )
    codex = JobTypeDispatcher(
        {
            "CODEX_RUN": InitialCodexExecutor(
                database, runner, timeout_seconds=config.codex.execution_timeout_seconds
            ).execute,
            "CODEX_REVIEW_REWORK": ReviewReworkCodexExecutor(
                database, runner, timeout_seconds=config.codex.execution_timeout_seconds
            ).execute,
        }
    )
    git = JobTypeDispatcher(
        {
            "WORKSPACE_PREPARE": WorkspacePrepareExecutor(database, workspace).execute,
            "CHANGE_VALIDATE": ChangeValidationExecutor(
                database, trusted_git, data_root
            ).execute,
            "GIT_COMMIT": TrustedCommitExecutor(
                database, trusted_git, data_root
            ).execute,
            "GIT_PUSH": TrustedPushExecutor(database, trusted_git, data_root).execute,
        }
    )
    initial_git = SubprocessInitialBaselineGit(
        data_root / "provisioning",
        github_username=config.github.owner,
        github_token=config.secrets.github_token,
    )
    github = JobTypeDispatcher(
        {
            "REPOSITORY_PROVISION": RepositoryProvisioningExecutor(
                database,
                lambda connection: RepositoryProvisioningService(
                    connection,
                    GitHubProvisioningAdapter(config),
                    initial_git,
                    owner=config.github.owner or "",
                    security_policy=SecurityPolicy(connection),
                ),
            ).execute,
            "PR_CREATE": PullRequestCreateExecutor(
                database,
                lambda connection: PullRequestLifecycleService(
                    connection, workspace(connection), github_prs
                ),
            ).execute,
            "PR_MERGE": GatekeeperMergeExecutor(
                database,
                lambda connection: Gatekeeper(
                    connection,
                    github_prs,
                    security_blocked=SecurityPolicy(connection).blocked,
                ),
            ).execute,
        }
    )
    ci = JobTypeDispatcher(
        {
            "CI_RECONCILE": CIReconciliationExecutor(
                database,
                lambda connection: CIMonitor(
                    connection,
                    github_prs,
                    GitHubActionsAdapter(config, metrics=recorder),
                ),
            ).execute
        }
    )
    result = {
        WorkerClass.ARCHITECT: architect,
        WorkerClass.CODEX: codex,
        WorkerClass.GIT: git,
        WorkerClass.GITHUB: github,
        WorkerClass.CI: ci,
    }
    validate_production_executors(result)
    return result


def validate_production_executors(executors: Mapping[WorkerClass, object]) -> None:
    if set(executors) != set(PRODUCTION_JOB_TYPES):
        raise ConfigurationError("production executor worker classes are incomplete")
    seen: set[str] = set()
    for worker, expected in PRODUCTION_JOB_TYPES.items():
        dispatcher = executors[worker]
        if (
            not isinstance(dispatcher, JobTypeDispatcher)
            or dispatcher.job_types != expected
        ):
            raise ConfigurationError(f"production {worker.value} routes are incomplete")
        if seen & dispatcher.job_types:
            raise ConfigurationError("a production job type has multiple worker owners")
        seen.update(dispatcher.job_types)


class ProductionDueWorkCoordinator:
    """Locally enqueue ordinary lifecycle work and CI bootstrap/poll work."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.lifecycle = LifecycleCoordinator(connection)
        self.ci = CIJobCoordinator(connection)

    def enqueue_due(self, now: datetime) -> None:
        self.lifecycle.enqueue_due()
        rows = self.connection.execute(
            """SELECT m.project_id,m.id,p.id AS pull_request_id,p.head_sha
               FROM milestones m JOIN pull_requests p ON p.id=m.active_pull_request_id
               WHERE m.state='CI_RUNNING' AND p.state='OPEN' ORDER BY m.id"""
        ).fetchall()
        for row in rows:
            from syntra_build.domain.identifiers import MilestoneId, ProjectId

            self.ci.bootstrap(
                ProjectId.from_string(row["project_id"]),
                MilestoneId.from_string(row["id"]),
                f"ci-bootstrap:{row['pull_request_id']}:{row['head_sha']}",
                now,
            )
        self.ci.enqueue_due(now)
