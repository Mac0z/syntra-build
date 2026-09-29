# ruff: noqa: E501
"""Thin, thread-owned Scheduler adapters for released M32 services."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.application.architect import (
    ArchitectDesignService,
    ArchitectError,
    ArchitectFailureKind,
    ArchitectProvider,
    ArchitectTaskService,
)
from syntra_build.application.codex import CodexRunner
from syntra_build.application.provisioning import (
    ProvisioningError,
    ProvisioningFailure,
    RepositoryProvisioningService,
)
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
)
from syntra_build.application.specification import SpecificationDraftService
from syntra_build.domain import (
    ARCHITECT_TASK_INTERFACE_VERSION,
    ArchitectTaskRequest,
    ArchitectTaskType,
    DesignPackageId,
    DesignPackageStatus,
    DocumentStatus,
    DocumentType,
    Job,
    Milestone,
    MilestoneId,
    MilestoneState,
    MilestoneTransitionRequest,
    ProjectState,
    RepositoryProvisioningStatus,
    WorkerClass,
)
from syntra_build.domain.codex import CodexProcessStatus, CodexRunRequest
from syntra_build.domain.failures import FailureClassification
from syntra_build.infrastructure.persistence.architect import (
    SQLiteArchitectInteractionRepository,
)
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.design import (
    SQLiteProjectDocumentRepository,
)
from syntra_build.infrastructure.persistence.design_packages import (
    SQLiteDesignPackageRepository,
)
from syntra_build.infrastructure.persistence.errors import PersistenceError
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository
from syntra_build.infrastructure.persistence.provisioning import (
    SQLiteProvisioningRepository,
)

_TRANSIENT_ARCHITECT_FAILURES = {
    ArchitectFailureKind.TIMEOUT,
    ArchitectFailureKind.TRANSIENT_PROVIDER,
    ArchitectFailureKind.THROTTLED,
}


class ArchitectTaskExecutor:
    """Reconstruct and execute one initial Architect task from durable truth."""

    def __init__(
        self,
        database_path: Path,
        provider: ArchitectProvider,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.provider = provider
        self.clock = clock
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "ARCHITECT_TASK":
            raise ValueError("Architect task execution requires an ARCHITECT_TASK job")
        if job.worker_class is not WorkerClass.ARCHITECT:
            raise ValueError("Architect task execution requires an Architect worker")
        if job.milestone_id is None:
            raise ValueError("Architect task execution requires a milestone-scoped job")
        if job.payload != {"task_type": ArchitectTaskType.IMPLEMENT.value}:
            raise ValueError("Architect task payload must be exactly IMPLEMENT")

        with closing(self.connection_factory(self.database_path)) as connection:
            interactions = SQLiteArchitectInteractionRepository(connection)
            milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
            milestone = milestones.get(job.milestone_id, job.project_id)
            project = SQLiteProjectRepository(connection, lambda: str(uuid4())).get(
                job.project_id
            )
            if project.state is not ProjectState.BUILDING:
                raise ValueError("Architect task project must be BUILDING")

            if self._accepted_for_job(interactions, job):
                if milestone.state is MilestoneState.PREPARING_TASK:
                    self._advance(milestones, job)
                elif milestone.state is not MilestoneState.PREPARING_WORKSPACE:
                    raise ValueError(
                        "accepted Architect task has an inconsistent milestone state"
                    )
                return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)

            if milestone.state is not MilestoneState.PREPARING_TASK:
                raise ValueError("Architect task milestone must be PREPARING_TASK")
            request = self._request(connection, job, milestone)
            service = ArchitectTaskService(
                interactions, self.provider, clock=self.clock
            )
            try:
                service.create(request)
            except ArchitectError as error:
                return JobExecutionResult(
                    JobExecutionDisposition.FAILED,
                    error_id=f"architect-task-{error.kind.value.casefold()}",
                    failure_classification=(
                        FailureClassification.TRANSIENT
                        if error.kind in _TRANSIENT_ARCHITECT_FAILURES
                        else FailureClassification.PERMANENT
                    ),
                )
            self._advance(milestones, job)
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)

    @staticmethod
    def _accepted_for_job(
        interactions: SQLiteArchitectInteractionRepository, job: Job
    ) -> bool:
        try:
            task = interactions.accepted_task_for_job(str(job.id))
        except PersistenceError:
            return False
        if (
            task.project_id != job.project_id
            or task.milestone_id != job.milestone_id
            or task.correlation_id != job.correlation_id
            or task.task_type is not ArchitectTaskType.IMPLEMENT
        ):
            raise ValueError(
                "accepted Architect task identity does not match Scheduler job"
            )
        return True

    def _request(
        self, connection: sqlite3.Connection, job: Job, milestone: Milestone
    ) -> ArchitectTaskRequest:
        row = connection.execute(
            """SELECT id FROM design_packages
               WHERE project_id=? AND status='APPROVED'
               ORDER BY approved_at DESC,id DESC LIMIT 1""",
            (str(job.project_id),),
        ).fetchone()
        if row is None:
            raise ValueError("approved design package is unavailable")
        package = SQLiteDesignPackageRepository(connection).get(
            DesignPackageId.from_string(row["id"])
        )
        if (
            package.project_id != job.project_id
            or package.status is not DesignPackageStatus.APPROVED
        ):
            raise ValueError("approved design package identity is inconsistent")

        documents = SQLiteProjectDocumentRepository(connection)
        spec = documents.get(job.project_id, package.spec_document_id)
        agents = documents.get(job.project_id, package.agents_document_id)
        if (
            spec.document_type is not DocumentType.SPEC
            or agents.document_type is not DocumentType.AGENTS
            or spec.status is not DocumentStatus.APPROVED
            or agents.status is not DocumentStatus.APPROVED
        ):
            raise ValueError(
                "design package does not reference approved SPEC and AGENTS"
            )

        if milestone.sequence_number >= len(package.planned_milestones):
            raise ValueError("milestone is absent from approved planning evidence")
        planned = package.planned_milestones[milestone.sequence_number]
        if (planned.code, planned.title) != (milestone.code, milestone.title):
            raise ValueError("milestone does not match approved planning evidence")

        provisioning = SQLiteProvisioningRepository(connection)
        repository = provisioning.for_project(job.project_id)
        baseline = provisioning.baseline(job.project_id)
        if (
            repository is None
            or repository.status is not RepositoryProvisioningStatus.VERIFIED
            or repository.verified_at is None
            or repository.external_repository_id is None
            or repository.default_branch is None
            or baseline is None
            or baseline.verified_at is None
            or baseline.github_repository_id != repository.id
            or baseline.spec_document_id != spec.id
            or baseline.agents_document_id != agents.id
            or baseline.spec_revision != spec.revision
            or baseline.agents_revision != agents.revision
            or baseline.spec_content_hash != spec.content_hash
            or baseline.agents_content_hash != agents.content_hash
        ):
            raise ValueError(
                "verified repository baseline is unavailable or inconsistent"
            )

        # No released durable milestone-summary artifact exists yet.  An empty
        # tuple is safer than synthesising history from milestone titles.
        return ArchitectTaskRequest(
            ARCHITECT_TASK_INTERFACE_VERSION,
            job.correlation_id,
            job.project_id,
            MilestoneId.from_string(str(job.milestone_id)),
            job.id,
            ArchitectTaskType.IMPLEMENT,
            str(spec.revision),
            spec.content,
            str(agents.revision),
            agents.content,
            {
                "id": str(milestone.id),
                "code": milestone.code,
                "title": milestone.title,
                "sequence_number": milestone.sequence_number,
                "state": milestone.state.value,
            },
            {
                "provider": repository.provider,
                "owner": repository.owner,
                "repository_name": repository.repository_name,
                "full_name": repository.full_name,
                "default_branch": repository.default_branch,
                "visibility": repository.visibility.value,
                "external_repository_id": repository.external_repository_id,
                "provisioning_status": repository.status.value,
                "baseline_commit_sha": baseline.commit_sha,
            },
            (),
            None,
        )

    def _advance(self, milestones: SQLiteMilestoneRepository, job: Job) -> None:
        occurred_at = self.clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("Architect task clock must return a UTC timestamp")
        assert job.milestone_id is not None
        milestones.apply_transition(
            MilestoneTransitionRequest(
                job.milestone_id,
                job.project_id,
                MilestoneState.PREPARING_TASK,
                MilestoneState.PREPARING_WORKSPACE,
                "accepted implementation task persisted",
                "SYSTEM",
                "architect-task-executor",
                job.correlation_id,
                occurred_at.astimezone(UTC),
            )
        )


class RepositoryProvisioningExecutor:
    """Run released M18 provisioning on a worker-owned connection."""

    def __init__(
        self,
        database_path: Path,
        service_factory: Callable[[sqlite3.Connection], RepositoryProvisioningService],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.service_factory = service_factory
        self.clock = clock
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "REPOSITORY_PROVISION":
            raise ValueError(
                "Repository provisioning requires a REPOSITORY_PROVISION job"
            )
        if job.worker_class is not WorkerClass.GITHUB:
            raise ValueError("Repository provisioning requires a GitHub worker")
        if job.milestone_id is not None:
            raise ValueError("Repository provisioning requires a project-scoped job")

        occurred_at = self.clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError(
                "Repository provisioning clock must return a UTC timestamp"
            )
        occurred_at = occurred_at.astimezone(UTC)
        try:
            with closing(self.connection_factory(self.database_path)) as connection:
                result = self.service_factory(connection).provision(
                    job.project_id, occurred_at, job.correlation_id
                )
        except ProvisioningError as error:
            return JobExecutionResult(
                JobExecutionDisposition.FAILED,
                error_id=f"repository-provision-{error.failure.value.casefold()}",
                failure_classification=(
                    FailureClassification.TRANSIENT
                    if error.failure is ProvisioningFailure.TRANSIENT
                    else FailureClassification.PERMANENT
                ),
            )
        if not result.verified or result.project_state is not ProjectState.READY:
            raise RuntimeError(
                "repository provisioning returned without durable READY verification"
            )
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)


class ArchitectDesignExecutor:
    """Run one persisted project design request on a worker-owned connection."""

    def __init__(
        self,
        database_path: Path,
        service_factory: Callable[[sqlite3.Connection], ArchitectDesignService],
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.service_factory = service_factory
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "ARCHITECT_DESIGN":
            raise ValueError("Architect design requires an ARCHITECT_DESIGN job")
        if job.worker_class is not WorkerClass.ARCHITECT:
            raise ValueError("Architect design requires an Architect worker")
        if job.milestone_id is not None:
            raise ValueError("Architect design requires a project-scoped job")

        # Scheduler calls execute on its worker pool. Constructing the service from
        # this connection keeps every SQLite repository on the executing thread.
        try:
            with closing(self.connection_factory(self.database_path)) as connection:
                self.service_factory(connection).design(
                    job.project_id, job.correlation_id
                )
        except ArchitectError as error:
            transient = {
                ArchitectFailureKind.TIMEOUT,
                ArchitectFailureKind.TRANSIENT_PROVIDER,
                ArchitectFailureKind.THROTTLED,
            }
            return JobExecutionResult(
                JobExecutionDisposition.FAILED,
                error_id=f"architect-design-{error.kind.value.casefold()}",
                failure_classification=(
                    FailureClassification.TRANSIENT
                    if error.kind in transient
                    else FailureClassification.PERMANENT
                ),
            )
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)


class SpecificationDraftExecutor:
    """Generate one project specification on a worker-owned connection."""

    def __init__(
        self,
        database_path: Path,
        service_factory: Callable[[sqlite3.Connection], SpecificationDraftService],
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.service_factory = service_factory
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "SPECIFICATION_DRAFT":
            raise ValueError(
                "Specification drafting requires a SPECIFICATION_DRAFT job"
            )
        if job.worker_class is not WorkerClass.ARCHITECT:
            raise ValueError("Specification drafting requires an Architect worker")
        if job.milestone_id is not None:
            raise ValueError("Specification drafting requires a project-scoped job")

        try:
            with closing(self.connection_factory(self.database_path)) as connection:
                self.service_factory(connection).generate(
                    job.project_id, job.correlation_id
                )
        except ArchitectError as error:
            transient = {
                ArchitectFailureKind.TIMEOUT,
                ArchitectFailureKind.TRANSIENT_PROVIDER,
                ArchitectFailureKind.THROTTLED,
            }
            return JobExecutionResult(
                JobExecutionDisposition.FAILED,
                error_id=f"specification-draft-{error.kind.value.casefold()}",
                failure_classification=(
                    FailureClassification.TRANSIENT
                    if error.kind in transient
                    else FailureClassification.PERMANENT
                ),
            )
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)


class InitialCodexExecutor:
    """Load accepted TASK evidence on the worker thread, then invoke M20."""

    def __init__(
        self,
        database_path: Path,
        runner_factory: Callable[[sqlite3.Connection], CodexRunner],
        *,
        timeout_seconds: float,
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.runner_factory = runner_factory
        self.timeout_seconds = timeout_seconds
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.worker_class is not WorkerClass.CODEX or job.milestone_id is None:
            raise ValueError("CODEX_RUN requires a Codex milestone job")
        with closing(self.connection_factory(self.database_path)) as connection:
            task = SQLiteArchitectInteractionRepository(
                connection
            ).accepted_task_for_milestone(str(job.project_id), str(job.milestone_id))
            row = connection.execute(
                """SELECT w.worktree_path,d.content FROM git_workspaces w
                   JOIN design_packages p ON p.project_id=w.project_id AND p.status='APPROVED'
                   JOIN project_documents d ON d.id=p.agents_document_id
                   WHERE w.project_id=? AND w.milestone_id=? AND w.state='READY'""",
                (str(job.project_id), str(job.milestone_id)),
            ).fetchone()
            if row is None:
                raise ValueError("accepted AGENTS or assigned workspace is unavailable")
            result = self.runner_factory(connection).run(
                CodexRunRequest(
                    "1.0",
                    job.correlation_id,
                    job.project_id,
                    job.milestone_id,
                    job.id,
                    job.attempt_number + 1,
                    Path(row["worktree_path"]),
                    task.to_dict(),
                    row["content"],
                    self.timeout_seconds,
                )
            )
        disposition = (
            JobExecutionDisposition.SUCCEEDED
            if result.process_status is CodexProcessStatus.SUCCEEDED
            else JobExecutionDisposition.FAILED
        )
        return JobExecutionResult(
            disposition,
            exit_code=result.exit_code,
            logs_reference=result.stdout_reference
            if disposition is JobExecutionDisposition.SUCCEEDED
            else result.stderr_reference,
        )
