"""Application boundary and M19 workspace binding for coding agents."""

from __future__ import annotations

from typing import Protocol

from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain.codex import CodexRunRequest, CodexRunResult
from syntra_build.domain.workspaces import WorkspaceError, WorkspaceState


class CodexRunner(Protocol):
    def run(self, request: CodexRunRequest) -> CodexRunResult: ...

    def cancel(self, job_id: str, attempt_number: int) -> bool: ...


class WorkspaceBoundCodexRunner(CodexRunner, Protocol):
    """Coding runner that can prove its released M19 workspace binding."""

    def run(
        self, request: CodexRunRequest, *, require_clean: bool = True
    ) -> CodexRunResult: ...

    def validate_workspace(
        self, request: CodexRunRequest, *, require_clean: bool
    ) -> None: ...


class BoundCodexRunner:
    """Reject caller paths and identities unless M19 evidence proves them."""

    def __init__(self, workspaces: WorkspaceService, provider: CodexRunner) -> None:
        self._workspaces = workspaces
        self._provider = provider

    def validate_workspace(
        self, request: CodexRunRequest, *, require_clean: bool
    ) -> None:
        inspection = self._workspaces.inspect(request.project_id, request.milestone_id)
        persisted = inspection.workspace.path.resolve(strict=True)
        supplied = request.worktree_path.resolve(strict=True)
        if supplied != persisted:
            raise WorkspaceError("Codex request path is not the persisted M19 worktree")
        if require_clean and inspection.workspace.state is not WorkspaceState.READY:
            raise WorkspaceError("Codex workspace is not clean and READY")
        # inspect() also proves root containment, repository registration, remote,
        # branch and authoritative HEAD before any untrusted process is started.

    def run(
        self, request: CodexRunRequest, *, require_clean: bool = True
    ) -> CodexRunResult:
        self.validate_workspace(request, require_clean=require_clean)
        return self._provider.run(request)

    def cancel(self, job_id: str, attempt_number: int) -> bool:
        return self._provider.cancel(job_id, attempt_number)
