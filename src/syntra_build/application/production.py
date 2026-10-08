"""Reviewed production composition for the released M32 scheduler routes."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import uuid4

from syntra_build.adapters.architect import OpenAIArchitectProvider
from syntra_build.adapters.github.actions import GitHubActionsAdapter
from syntra_build.adapters.github.provisioning import GitHubProvisioningAdapter
from syntra_build.adapters.github.pull_requests import GitHubPullRequestAdapter
from syntra_build.adapters.telegram import TelegramClient, TelegramGateNotifier
from syntra_build.adapters.telegram.gates import classify_notification_failure
from syntra_build.application.architect import ArchitectDesignService, ArchitectProvider
from syntra_build.application.architect_review import (
    ArchitectReviewService,
    ReviewArchitectProvider,
    ReviewContextGateway,
    ReviewPullRequestGateway,
)
from syntra_build.application.ci_monitor import CIActionsGateway, CIMonitor
from syntra_build.application.ci_scheduler import (
    CIJobCoordinator,
    CIReconciliationExecutor,
)
from syntra_build.application.codex import BoundCodexRunner, WorkspaceBoundCodexRunner
from syntra_build.application.design import ProjectDesignContextService
from syntra_build.application.design_notifications import (
    DesignApprovalNotificationExecutor,
)
from syntra_build.application.gatekeeper import Gatekeeper, GitHubMergeGateway
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
from syntra_build.application.pull_requests import (
    GitHubPullRequestGateway,
    PullRequestLifecycleService,
)
from syntra_build.application.review_rework import ReviewReworkCodexExecutor
from syntra_build.application.security import SecurityPolicy
from syntra_build.application.specification import SpecificationDraftService
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain import ProjectCreationContext
from syntra_build.domain.jobs import WorkerClass
from syntra_build.infrastructure.codex_runner import LocalCodexCliRunner
from syntra_build.infrastructure.config.models import (
    ApplicationConfig,
    ConfigurationError,
)
from syntra_build.infrastructure.git_initial import SubprocessInitialBaselineGit
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.metrics import PrometheusRecorder
from syntra_build.infrastructure.persistence import (
    SQLiteArchitectInteractionRepository,
    SQLiteDesignMessageRepository,
    SQLiteDesignPackageRepository,
    SQLiteHumanGateRepository,
    SQLiteProjectDecisionRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    SQLiteTelegramGateNotificationRepository,
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
    WorkerClass.MESSAGING: frozenset({"DESIGN_APPROVAL_NOTIFY"}),
}


@dataclass(frozen=True, slots=True)
class ProductionDependencies:
    """Narrow external-boundary overrides for composition and acceptance tests."""

    architect_provider_factory: Callable[[], ArchitectProvider] | None = None
    codex_runner_factory: (
        Callable[[sqlite3.Connection], WorkspaceBoundCodexRunner] | None
    ) = None
    trusted_git: TrustedGit | None = None
    github_pull_requests: GitHubPullRequestGateway | None = None
    github_actions_factory: Callable[[], CIActionsGateway] | None = None
    telegram_client_factory: Callable[[], TelegramClient] | None = None


def _context(connection: sqlite3.Connection) -> ProjectDesignContextService:
    projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
    return ProjectDesignContextService(
        projects,
        SQLiteDesignMessageRepository(connection),
        SQLiteProjectDecisionRepository(connection),
        SQLiteProjectDocumentRepository(connection),
    )


def build_production_executors(
    config: ApplicationConfig,
    *,
    metrics: PrometheusRecorder | None = None,
    dependencies: ProductionDependencies | None = None,
) -> Mapping[WorkerClass, JobTypeDispatcher]:
    """Construct the exact closed M32 vocabulary without opening worker databases."""
    dependencies = dependencies or ProductionDependencies()
    if dependencies.architect_provider_factory is None and (
        not config.architect.enabled
        or config.architect.provider != "openai"
        or not config.architect.model
        or config.secrets.architect_api_key is None
    ):
        raise ConfigurationError(
            "the configured OpenAI Architect integration is required"
        )
    if dependencies.github_pull_requests is None and (
        not config.github.enabled
        or not config.github.owner
        or config.secrets.github_token is None
    ):
        raise ConfigurationError("the configured GitHub integration is required")
    if dependencies.codex_runner_factory is None and (
        not config.codex.executable.strip()
        or shutil.which(config.codex.executable) is None
    ):
        raise ConfigurationError("the configured Codex executable is required")

    database = config.database.sqlite_path
    data_root = config.filesystem.data_root
    recorder = metrics or PrometheusRecorder()
    trusted_git = dependencies.trusted_git or trusted_git_from_host_config(
        config, data_root
    )
    github_prs = dependencies.github_pull_requests or GitHubPullRequestAdapter(config)

    def architect_provider() -> ArchitectProvider:
        if dependencies.architect_provider_factory is not None:
            return dependencies.architect_provider_factory()
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

    def design_notifier(
        connection: sqlite3.Connection, context: ProjectCreationContext
    ) -> TelegramGateNotifier:
        if context.messaging_platform != "telegram":
            raise ValueError("design approval requires persisted Telegram context")
        return TelegramGateNotifier(
            dependencies.telegram_client_factory()
            if dependencies.telegram_client_factory is not None
            else TelegramClient(config, metrics=recorder),
            chat_id=int(context.conversation_id),
            thread_id=int(context.thread_id) if context.thread_id is not None else None,
            notifications=SQLiteTelegramGateNotificationRepository(connection),
        )

    def runner(connection: sqlite3.Connection) -> WorkspaceBoundCodexRunner:
        if dependencies.codex_runner_factory is not None:
            return dependencies.codex_runner_factory(connection)
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
            cast(ReviewPullRequestGateway, github_prs),
            cast(ReviewContextGateway, github_prs),
            cast(ReviewArchitectProvider, architect_provider()),
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
                    cast(GitHubMergeGateway, github_prs),
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
                    dependencies.github_actions_factory()
                    if dependencies.github_actions_factory is not None
                    else GitHubActionsAdapter(config, metrics=recorder),
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
        WorkerClass.MESSAGING: JobTypeDispatcher(
            {
                "DESIGN_APPROVAL_NOTIFY": DesignApprovalNotificationExecutor(
                    database,
                    design_notifier,
                    failure_classifier=classify_notification_failure,
                ).execute
            }
        ),
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

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.connection = connection
        self.lifecycle = (
            LifecycleCoordinator(connection)
            if clock is None
            else LifecycleCoordinator(connection, clock=clock)
        )
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
