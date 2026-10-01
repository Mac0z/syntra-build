# ruff: noqa: E501
"""Thin, thread-owned Scheduler adapters for released M32 services."""

from __future__ import annotations

import json
import re
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
from syntra_build.application.change_validation import ChangeValidationService
from syntra_build.application.codex import WorkspaceBoundCodexRunner
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
from syntra_build.application.workspaces import WorkspaceService, milestone_branch_name
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
from syntra_build.domain.change_validation import ValidationDecision
from syntra_build.domain.codex import CodexProcessStatus, CodexRunRequest
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.workspaces import (
    PushNotAppliedError,
    Workspace,
    WorkspaceError,
    WorkspaceState,
)
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence.architect import (
    SQLiteArchitectInteractionRepository,
)
from syntra_build.infrastructure.persistence.change_validation import (
    AcceptedChangeSetEvidence,
    SQLiteValidationRepository,
)
from syntra_build.infrastructure.persistence.codex import SQLiteCodexRunRepository
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
from syntra_build.infrastructure.persistence.workspaces import (
    PersistedTrustedCommit,
    SQLiteWorkspaceRepository,
)

_CANONICAL_DIFF_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")

_TRANSIENT_ARCHITECT_FAILURES = {
    ArchitectFailureKind.TIMEOUT,
    ArchitectFailureKind.TRANSIENT_PROVIDER,
    ArchitectFailureKind.THROTTLED,
}


class WorkspacePrepareExecutor:
    """Prepare and verify one trusted M19 workspace on the worker thread."""

    def __init__(
        self,
        database_path: Path,
        service_factory: Callable[[sqlite3.Connection], WorkspaceService],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.service_factory = service_factory
        self.clock = clock
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "WORKSPACE_PREPARE":
            raise ValueError("Workspace preparation requires a WORKSPACE_PREPARE job")
        if job.worker_class is not WorkerClass.GIT:
            raise ValueError("Workspace preparation requires a Git worker")
        if job.milestone_id is None:
            raise ValueError("Workspace preparation requires a milestone-scoped job")
        if job.payload:
            raise ValueError("Workspace preparation does not accept workflow payload")

        occurred_at = self.clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("Workspace preparation clock must return a UTC timestamp")
        occurred_at = occurred_at.astimezone(UTC)

        try:
            with closing(self.connection_factory(self.database_path)) as connection:
                milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
                milestone = milestones.get(job.milestone_id, job.project_id)
                project = SQLiteProjectRepository(connection, lambda: str(uuid4())).get(
                    job.project_id
                )
                if project.state is not ProjectState.BUILDING:
                    raise ValueError("Workspace preparation project must be BUILDING")
                if milestone.state not in {
                    MilestoneState.PREPARING_WORKSPACE,
                    MilestoneState.CODING,
                }:
                    raise ValueError(
                        "Workspace preparation milestone must be PREPARING_WORKSPACE"
                    )

                task = SQLiteArchitectInteractionRepository(
                    connection
                ).accepted_task_for_milestone(
                    str(job.project_id), str(job.milestone_id)
                )
                if (
                    task.project_id != job.project_id
                    or task.milestone_id != job.milestone_id
                    or task.task_type is not ArchitectTaskType.IMPLEMENT
                ):
                    raise ValueError(
                        "accepted Architect IMPLEMENT task identity is inconsistent"
                    )

                provisioning = SQLiteProvisioningRepository(connection)
                repository = provisioning.for_project(job.project_id)
                baseline = provisioning.baseline(job.project_id)
                if (
                    repository is None
                    or repository.status is not RepositoryProvisioningStatus.VERIFIED
                    or repository.verified_at is None
                    or repository.external_repository_id is None
                    or repository.default_branch != "main"
                    or baseline is None
                    or baseline.verified_at is None
                    or baseline.github_repository_id != repository.id
                ):
                    raise ValueError(
                        "verified M18 repository baseline is unavailable or inconsistent"
                    )

                service = self.service_factory(connection)
                workspace = service.prepare_workspace(
                    job.project_id, job.milestone_id, occurred_at
                )
                persisted = SQLiteWorkspaceRepository(
                    connection
                ).workspace_for_milestone(job.milestone_id)
                expected_branch = milestone_branch_name(
                    milestone.sequence_number, milestone.title
                )
                expected_path = (
                    service.workspace_root / str(job.project_id) / str(job.milestone_id)
                ).resolve(strict=False)
                if (
                    persisted != workspace
                    or workspace.project_id != job.project_id
                    or workspace.milestone_id != job.milestone_id
                    or workspace.path != expected_path
                    or service.workspace_root not in workspace.path.parents
                    or workspace.branch_name != expected_branch
                    or workspace.base_branch != "main"
                    or not workspace.base_sha
                    or workspace.last_validated_at is None
                    or workspace.state is not WorkspaceState.READY
                    or workspace.current_head_sha != workspace.base_sha
                ):
                    raise WorkspaceError(
                        "prepared workspace is not verified ready for implementation"
                    )

                if milestone.state is MilestoneState.PREPARING_WORKSPACE:
                    milestones.apply_transition(
                        MilestoneTransitionRequest(
                            job.milestone_id,
                            job.project_id,
                            MilestoneState.PREPARING_WORKSPACE,
                            MilestoneState.CODING,
                            "trusted workspace prepared and verified",
                            "SYSTEM",
                            "workspace-prepare-executor",
                            job.correlation_id,
                            occurred_at,
                        )
                    )
        except WorkspaceError:
            return JobExecutionResult(
                JobExecutionDisposition.FAILED,
                error_id="workspace-prepare-invariant",
                failure_classification=FailureClassification.PERMANENT,
            )
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)


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
    """Execute the initial IMPLEMENT task and durably advance its milestone."""

    def __init__(
        self,
        database_path: Path,
        runner_factory: Callable[[sqlite3.Connection], WorkspaceBoundCodexRunner],
        *,
        timeout_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.runner_factory = runner_factory
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "CODEX_RUN":
            raise ValueError("initial Codex execution requires a CODEX_RUN job")
        if job.worker_class is not WorkerClass.CODEX:
            raise ValueError("initial Codex execution requires a Codex worker")
        if job.milestone_id is None:
            raise ValueError("initial Codex execution requires a milestone-scoped job")
        if job.payload:
            raise ValueError("initial Codex execution does not accept workflow payload")
        with closing(self.connection_factory(self.database_path)) as connection:
            milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
            milestone = milestones.get(job.milestone_id, job.project_id)
            project = SQLiteProjectRepository(connection, lambda: str(uuid4())).get(
                job.project_id
            )
            if project.state is not ProjectState.BUILDING:
                raise ValueError("initial Codex project must be BUILDING")
            if milestone.state not in {
                MilestoneState.CODING,
                MilestoneState.VALIDATING_CHANGES,
            }:
                raise ValueError("initial Codex milestone has an inconsistent state")
            task = SQLiteArchitectInteractionRepository(
                connection
            ).accepted_task_for_milestone(str(job.project_id), str(job.milestone_id))
            if (
                task.project_id != job.project_id
                or task.milestone_id != job.milestone_id
                or task.task_type is not ArchitectTaskType.IMPLEMENT
            ):
                raise ValueError("accepted Architect IMPLEMENT task is inconsistent")
            row = connection.execute(
                """SELECT w.worktree_path,w.state AS workspace_state,d.content,
                          d.project_id AS document_project_id,
                          d.document_type,d.status AS document_status
                   FROM git_workspaces w
                   JOIN design_packages p ON p.project_id=w.project_id
                     AND p.status='APPROVED'
                   JOIN project_documents d ON d.id=p.agents_document_id
                   WHERE w.project_id=? AND w.milestone_id=?
                   ORDER BY p.approved_at DESC,p.id DESC LIMIT 1""",
                (str(job.project_id), str(job.milestone_id)),
            ).fetchone()
            if row is None:
                raise ValueError("accepted AGENTS or assigned workspace is unavailable")
            if (
                row["document_project_id"] != str(job.project_id)
                or row["document_type"] != DocumentType.AGENTS.value
                or row["document_status"] != DocumentStatus.APPROVED.value
            ):
                raise ValueError("approved AGENTS identity is inconsistent")
            request = CodexRunRequest(
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
            runner = self.runner_factory(connection)
            runs = SQLiteCodexRunRepository(connection)
            result = runs.completed_for_request(request)
            if result is None and row["workspace_state"] != WorkspaceState.READY.value:
                raise ValueError("assigned workspace is not READY for Codex")
            # Replay permits expected uncommitted Codex output; a new untrusted
            # process additionally requires a clean READY workspace.
            runner.validate_workspace(request, require_clean=result is None)
            if result is None:
                if milestone.state is not MilestoneState.CODING:
                    raise ValueError(
                        "advanced milestone has no successful Codex evidence"
                    )
                result = runner.run(request)
                persisted = runs.completed_for_request(request)
                if persisted is None:
                    raise RuntimeError("Codex result was not durably persisted")
                self._require_result_identity(request, result)
                self._require_result_identity(request, persisted)
                if persisted.process_status is not result.process_status:
                    raise ValueError("Codex result disagrees with durable run evidence")
                result = persisted
            else:
                self._require_result_identity(request, result)

            if result.process_status is CodexProcessStatus.SUCCEEDED:
                if milestone.state is MilestoneState.CODING:
                    occurred_at = self.clock()
                    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                        raise ValueError(
                            "initial Codex clock must return a UTC timestamp"
                        )
                    milestones.apply_transition(
                        MilestoneTransitionRequest(
                            job.milestone_id,
                            job.project_id,
                            MilestoneState.CODING,
                            MilestoneState.VALIDATING_CHANGES,
                            "successful Codex run durably recorded",
                            "SYSTEM",
                            "initial-codex-executor",
                            job.correlation_id,
                            occurred_at.astimezone(UTC),
                        )
                    )
                disposition = JobExecutionDisposition.SUCCEEDED
                classification = None
            else:
                if milestone.state is not MilestoneState.CODING:
                    raise ValueError(
                        "unsuccessful Codex run cannot accompany advancement"
                    )
                disposition = JobExecutionDisposition.FAILED
                classification = {
                    CodexProcessStatus.TIMED_OUT: FailureClassification.TRANSIENT,
                    CodexProcessStatus.CANCELLED: FailureClassification.CANCELLED,
                }.get(result.process_status, FailureClassification.PERMANENT)
        return JobExecutionResult(
            disposition,
            exit_code=result.exit_code,
            logs_reference=result.stdout_reference
            if disposition is JobExecutionDisposition.SUCCEEDED
            else result.stderr_reference,
            failure_classification=classification,
        )

    @staticmethod
    def _require_result_identity(request: CodexRunRequest, result: object) -> None:
        if not all(
            getattr(result, field) == getattr(request, field)
            for field in (
                "interface_version",
                "correlation_id",
                "project_id",
                "milestone_id",
                "job_id",
                "attempt_number",
            )
        ):
            raise ValueError("Codex result identity does not match request")


class ChangeValidationExecutor:
    """Validate one Codex-produced worktree delta without mutating Git state."""

    def __init__(
        self,
        database_path: Path,
        git: TrustedGit,
        data_root: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.git = git
        self.data_root = data_root
        self.clock = clock
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "CHANGE_VALIDATE":
            raise ValueError("change validation requires a CHANGE_VALIDATE job")
        if job.worker_class is not WorkerClass.GIT:
            raise ValueError("change validation requires a Git worker")
        if job.milestone_id is None:
            raise ValueError("change validation requires a milestone-scoped job")
        if job.payload:
            raise ValueError("change validation does not accept workflow payload")

        occurred_at = self.clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("change validation clock must return a UTC timestamp")
        occurred_at = occurred_at.astimezone(UTC)

        with closing(self.connection_factory(self.database_path)) as connection:
            milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
            milestone = milestones.get(job.milestone_id, job.project_id)
            project = SQLiteProjectRepository(connection, lambda: str(uuid4())).get(
                job.project_id
            )
            if project.state is not ProjectState.BUILDING:
                raise ValueError("change validation project must be BUILDING")
            if milestone.state not in {
                MilestoneState.VALIDATING_CHANGES,
                MilestoneState.COMMITTING,
            }:
                raise ValueError(
                    "change validation milestone must be VALIDATING_CHANGES"
                )

            workspaces = SQLiteWorkspaceRepository(connection)
            workspace = workspaces.workspace_for_milestone(job.milestone_id)
            if (
                workspace is None
                or workspace.project_id != job.project_id
                or workspace.milestone_id != job.milestone_id
            ):
                raise ValueError("managed workspace identity is unavailable")
            if not SQLiteCodexRunRepository(connection).has_successful_coding_run(
                job.project_id, job.milestone_id, workspace.id
            ):
                raise ValueError("successful durable Codex run is unavailable")

            service = ChangeValidationService(connection, self.git, self.data_root)
            validations = SQLiteValidationRepository(connection)
            trusted_head = workspace.current_head_sha or workspace.base_sha
            live = service.replay_evidence(job.project_id, job.milestone_id)
            current = live.collected if live is not None else None
            accepted = (
                validations.accepted_evidence(
                    workspace.id, trusted_head, current.canonical_hash
                )
                if current is not None
                else None
            )
            if accepted is not None:
                assert current is not None
                self._require_accepted_identity(accepted, job, workspace, current.files)
                if milestone.state is MilestoneState.VALIDATING_CHANGES:
                    self._advance(milestones, job, occurred_at)
                return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)
            if milestone.state is MilestoneState.COMMITTING:
                raise ValueError(
                    "COMMITTING milestone has no accepted evidence for current diff"
                )

            change_set = service.validate(
                job.project_id,
                job.milestone_id,
                job.correlation_id,
                authorised_protected_paths=(),
                now=occurred_at,
            )
            if change_set.decision is ValidationDecision.ACCEPT:
                if (
                    change_set.project_id != job.project_id
                    or change_set.milestone_id != job.milestone_id
                    or change_set.correlation_id != job.correlation_id
                    or change_set.workspace_id != workspace.id
                    or change_set.branch_name != workspace.branch_name
                    or change_set.trusted_head_sha != trusted_head
                    or change_set.is_empty
                    or not change_set.files
                    or _CANONICAL_DIFF_HASH.fullmatch(change_set.diff_hash) is None
                ):
                    raise ValueError("accepted change set identity is inconsistent")
                durable = validations.accepted_evidence(
                    workspace.id, trusted_head, change_set.diff_hash
                )
                if durable is None:
                    raise RuntimeError("accepted change set was not durably persisted")
                self._require_accepted_identity(
                    durable, job, workspace, change_set.files
                )
                self._advance(milestones, job, occurred_at)
                return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)

            return JobExecutionResult(
                JobExecutionDisposition.FAILED,
                error_id=f"change-validation-{change_set.decision.value.casefold()}",
                failure_classification=(
                    FailureClassification.POLICY
                    if change_set.decision is ValidationDecision.BLOCKED
                    else FailureClassification.PERMANENT
                ),
            )

    @staticmethod
    def _require_accepted_identity(
        evidence: AcceptedChangeSetEvidence,
        job: Job,
        workspace: Workspace,
        files: tuple[object, ...],
    ) -> None:
        persisted_files = json.loads(evidence.files_json)
        if (
            evidence.project_id != str(job.project_id)
            or evidence.milestone_id != str(job.milestone_id)
            or evidence.correlation_id != job.correlation_id
            or evidence.workspace_id != workspace.id
            or evidence.branch_name != workspace.branch_name
            or evidence.trusted_head_sha
            != (workspace.current_head_sha or workspace.base_sha)
            or evidence.diff_hash is None
            or _CANONICAL_DIFF_HASH.fullmatch(evidence.diff_hash) is None
            or not files
            or not persisted_files
        ):
            raise ValueError("accepted validation evidence identity is inconsistent")

    @staticmethod
    def _advance(
        milestones: SQLiteMilestoneRepository,
        job: Job,
        occurred_at: datetime,
    ) -> None:
        assert job.milestone_id is not None
        milestones.apply_transition(
            MilestoneTransitionRequest(
                job.milestone_id,
                job.project_id,
                MilestoneState.VALIDATING_CHANGES,
                MilestoneState.COMMITTING,
                "worktree changes accepted by durable validation",
                "SYSTEM",
                "change-validation-executor",
                job.correlation_id,
                occurred_at,
            )
        )


class TrustedCommitExecutor:
    """Create or safely replay one validation-bound trusted local commit."""

    def __init__(
        self,
        database_path: Path,
        git: TrustedGit,
        data_root: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.git = git
        self.data_root = data_root
        self.clock = clock
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "GIT_COMMIT":
            raise ValueError("trusted commit requires a GIT_COMMIT job")
        if job.worker_class is not WorkerClass.GIT:
            raise ValueError("trusted commit requires a Git worker")
        if job.milestone_id is None:
            raise ValueError("trusted commit requires a milestone-scoped job")
        if job.payload:
            raise ValueError("trusted commit does not accept workflow payload")

        occurred_at = self.clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("trusted commit clock must return a UTC timestamp")
        occurred_at = occurred_at.astimezone(UTC)
        try:
            with closing(self.connection_factory(self.database_path)) as connection:
                milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
                milestone = milestones.get(job.milestone_id, job.project_id)
                project = SQLiteProjectRepository(connection, lambda: str(uuid4())).get(
                    job.project_id
                )
                if project.state is not ProjectState.BUILDING:
                    raise ValueError("trusted commit project must be BUILDING")
                if milestone.state not in {
                    MilestoneState.COMMITTING,
                    MilestoneState.PUSHING,
                }:
                    raise ValueError("trusted commit milestone must be COMMITTING")

                records = SQLiteWorkspaceRepository(connection)
                workspace = records.workspace_for_milestone(job.milestone_id)
                if (
                    workspace is None
                    or workspace.project_id != job.project_id
                    or workspace.milestone_id != job.milestone_id
                ):
                    raise ValueError("managed workspace identity is unavailable")

                trusted_head = workspace.current_head_sha or workspace.base_sha
                validations = SQLiteValidationRepository(connection)
                evidence = validations.accepted_for_workspace_head(
                    workspace.id, trusted_head
                )
                persisted = (
                    records.commit_for_change_set(workspace.id, evidence.id)
                    if evidence is not None
                    else None
                )
                accepted_head = trusted_head
                if evidence is None:
                    # A post-commit crash advances the durable workspace HEAD before
                    # the milestone transition. Replay is allowed only for the commit
                    # at that exact HEAD, and only when no newer ACCEPT exists there.
                    persisted = records.commit_at_head(workspace.id, trusted_head)
                    if persisted is not None:
                        evidence = validations.accepted_by_id(persisted.change_set_id)
                        accepted_head = persisted.commit.parent_sha
                paths = self._accepted_paths(evidence, job, workspace, accepted_head)
                assert evidence is not None
                message = f"{milestone.code}: {milestone.title}"
                service = WorkspaceService(connection, self.git, self.data_root)

                if persisted is None:
                    if milestone.state is not MilestoneState.COMMITTING:
                        raise ValueError(
                            "PUSHING milestone has no durable trusted commit"
                        )
                    created = service.commit(
                        job.project_id,
                        job.milestone_id,
                        accepted_head,
                        paths,
                        message,
                        occurred_at,
                        expected_diff_hash=evidence.diff_hash,
                    )
                    persisted = records.commit_for_change_set(workspace.id, evidence.id)
                    if persisted is None or (
                        persisted.commit.commit_sha != created.commit_sha
                    ):
                        raise WorkspaceError("trusted commit was not durably persisted")

                self._verify_commit(
                    persisted, job, workspace, evidence, message, service, occurred_at
                )
                if milestone.state is MilestoneState.COMMITTING:
                    milestones.apply_transition(
                        MilestoneTransitionRequest(
                            job.milestone_id,
                            job.project_id,
                            MilestoneState.COMMITTING,
                            MilestoneState.PUSHING,
                            "trusted local commit durably verified",
                            "SYSTEM",
                            "trusted-commit-executor",
                            job.correlation_id,
                            occurred_at,
                        )
                    )
        except WorkspaceError:
            return JobExecutionResult(
                JobExecutionDisposition.FAILED,
                error_id="trusted-commit-invariant",
                failure_classification=FailureClassification.PERMANENT,
            )
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)

    @staticmethod
    def _accepted_paths(
        evidence: AcceptedChangeSetEvidence | None,
        job: Job,
        workspace: Workspace,
        accepted_head: str,
    ) -> tuple[str, ...]:
        if evidence is None:
            raise WorkspaceError("accepted validation evidence is unavailable")
        try:
            files = json.loads(evidence.files_json)
            paths = tuple(item["path"] for item in files)
        except (TypeError, KeyError, json.JSONDecodeError) as error:
            raise WorkspaceError("accepted files evidence is malformed") from error
        if (
            evidence.project_id != str(job.project_id)
            or evidence.milestone_id != str(job.milestone_id)
            or evidence.workspace_id != workspace.id
            or evidence.branch_name != workspace.branch_name
            or evidence.trusted_head_sha != accepted_head
            or _CANONICAL_DIFF_HASH.fullmatch(evidence.diff_hash) is None
            or not paths
            or len(set(paths)) != len(paths)
            or not all(isinstance(path, str) and path for path in paths)
        ):
            raise WorkspaceError(
                "accepted validation evidence identity is inconsistent"
            )
        return paths

    @staticmethod
    def _verify_commit(
        persisted: PersistedTrustedCommit,
        job: Job,
        original: Workspace,
        evidence: AcceptedChangeSetEvidence,
        message: str,
        service: WorkspaceService,
        occurred_at: datetime,
    ) -> None:
        commit = persisted.commit
        if (
            persisted.project_id != job.project_id
            or persisted.milestone_id != job.milestone_id
            or commit.workspace_id != original.id
            or commit.parent_sha != evidence.trusted_head_sha
            or commit.branch_name != original.branch_name
            or commit.message != message
            or persisted.change_set_id != evidence.id
            or persisted.validated_diff_hash != evidence.diff_hash
            or commit.pushed_at is not None
        ):
            raise WorkspaceError("durable trusted commit identity is inconsistent")
        inspection = service.inspect(job.project_id, job.milestone_id, occurred_at)
        if (
            inspection.workspace.project_id != job.project_id
            or inspection.workspace.milestone_id != job.milestone_id
            or inspection.workspace.id != original.id
            or inspection.workspace.branch_name != original.branch_name
            or inspection.workspace.current_head_sha != commit.commit_sha
            or inspection.current_branch != commit.branch_name
            or inspection.head_sha != commit.commit_sha
        ):
            raise WorkspaceError("live workspace differs from durable trusted commit")


class TrustedPushExecutor:
    """Push and independently verify the trusted commit at the current HEAD."""

    def __init__(
        self,
        database_path: Path,
        git: TrustedGit,
        data_root: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.git = git
        self.data_root = data_root
        self.clock = clock
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "GIT_PUSH":
            raise ValueError("trusted push requires a GIT_PUSH job")
        if job.worker_class is not WorkerClass.GIT:
            raise ValueError("trusted push requires a Git worker")
        if job.milestone_id is None:
            raise ValueError("trusted push requires a milestone-scoped job")
        if job.payload:
            raise ValueError("trusted push does not accept workflow payload")

        occurred_at = self.clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("trusted push clock must return a UTC timestamp")
        occurred_at = occurred_at.astimezone(UTC)

        try:
            with closing(self.connection_factory(self.database_path)) as connection:
                milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
                milestone = milestones.get(job.milestone_id, job.project_id)
                project = SQLiteProjectRepository(connection, lambda: str(uuid4())).get(
                    job.project_id
                )
                if project.state is not ProjectState.BUILDING:
                    raise ValueError("trusted push project must be BUILDING")
                if milestone.state not in {
                    MilestoneState.PUSHING,
                    MilestoneState.PR_CREATING,
                }:
                    raise ValueError("trusted push milestone must be PUSHING")

                records = SQLiteWorkspaceRepository(connection)
                managed = records.managed_for_project(job.project_id)
                workspace = records.workspace_for_milestone(job.milestone_id)
                if (
                    managed is None
                    or workspace is None
                    or workspace.project_id != job.project_id
                    or workspace.milestone_id != job.milestone_id
                    or workspace.git_repository_id != managed.id
                ):
                    raise WorkspaceError("managed workspace identity is unavailable")
                if workspace.current_head_sha is None:
                    raise WorkspaceError("workspace has no current trusted HEAD")
                persisted = records.commit_at_head(
                    workspace.id, workspace.current_head_sha
                )
                if persisted is None:
                    raise WorkspaceError(
                        "trusted commit at current HEAD is unavailable"
                    )
                self._verify_chain(persisted, job, workspace, connection)

                service = WorkspaceService(connection, self.git, self.data_root)
                # Re-prove live local identity on every replay, including after a
                # durable push marker already exists.
                inspection = service.inspect(
                    job.project_id, job.milestone_id, occurred_at
                )
                if (
                    inspection.workspace.id != workspace.id
                    or inspection.current_branch != workspace.branch_name
                    or inspection.head_sha != persisted.commit.commit_sha
                ):
                    raise WorkspaceError(
                        "live workspace differs from trusted push target"
                    )

                result = None
                if (
                    milestone.state is MilestoneState.PUSHING
                    and persisted.commit.pushed_at is None
                ):
                    result = service.push(
                        job.project_id,
                        job.milestone_id,
                        persisted.commit.commit_sha,
                        occurred_at,
                    )
                    if (
                        result.branch_name != workspace.branch_name
                        or result.commit_sha != persisted.commit.commit_sha
                        or result.remote_sha != persisted.commit.commit_sha
                    ):
                        raise WorkspaceError(
                            "trusted push result identity is inconsistent"
                        )

                durable = records.commit_at_head(
                    workspace.id, workspace.current_head_sha
                )
                self._verify_durable(persisted, durable)
                remote_sha = self.git.remote_branch_sha(
                    managed.path, managed.remote_url, workspace.branch_name
                )
                if remote_sha != persisted.commit.commit_sha:
                    raise WorkspaceError(
                        "durable push evidence conflicts with remote branch"
                    )

                if milestone.state is MilestoneState.PUSHING:
                    milestones.apply_transition(
                        MilestoneTransitionRequest(
                            job.milestone_id,
                            job.project_id,
                            MilestoneState.PUSHING,
                            MilestoneState.PR_CREATING,
                            "trusted remote commit durably verified",
                            "SYSTEM",
                            "trusted-push-executor",
                            job.correlation_id,
                            occurred_at,
                        )
                    )
        except PushNotAppliedError:
            return JobExecutionResult(
                JobExecutionDisposition.FAILED,
                error_id="trusted-push-not-applied",
                failure_classification=FailureClassification.TRANSIENT,
            )
        except WorkspaceError:
            return JobExecutionResult(
                JobExecutionDisposition.FAILED,
                error_id="trusted-push-invariant",
                failure_classification=FailureClassification.PERMANENT,
            )
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)

    @staticmethod
    def _verify_chain(
        persisted: PersistedTrustedCommit,
        job: Job,
        workspace: Workspace,
        connection: sqlite3.Connection,
    ) -> None:
        commit = persisted.commit
        evidence = SQLiteValidationRepository(connection).accepted_by_id(
            persisted.change_set_id
        )
        if (
            persisted.project_id != job.project_id
            or persisted.milestone_id != job.milestone_id
            or commit.workspace_id != workspace.id
            or commit.branch_name != workspace.branch_name
            or commit.commit_sha != workspace.current_head_sha
            or evidence is None
            or evidence.project_id != str(job.project_id)
            or evidence.milestone_id != str(job.milestone_id)
            or evidence.workspace_id != workspace.id
            or evidence.branch_name != workspace.branch_name
            or evidence.trusted_head_sha != commit.parent_sha
            or evidence.diff_hash != persisted.validated_diff_hash
            or _CANONICAL_DIFF_HASH.fullmatch(persisted.validated_diff_hash) is None
        ):
            raise WorkspaceError("trusted commit validation linkage is inconsistent")

    @staticmethod
    def _verify_durable(
        expected: PersistedTrustedCommit,
        actual: PersistedTrustedCommit | None,
    ) -> None:
        if (
            actual is None
            or actual.project_id != expected.project_id
            or actual.milestone_id != expected.milestone_id
            or actual.commit.workspace_id != expected.commit.workspace_id
            or actual.commit.commit_sha != expected.commit.commit_sha
            or actual.commit.branch_name != expected.commit.branch_name
            or actual.change_set_id != expected.change_set_id
            or actual.validated_diff_hash != expected.validated_diff_hash
            or actual.commit.pushed_at is None
        ):
            raise WorkspaceError("durable trusted push evidence is inconsistent")
