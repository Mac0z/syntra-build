"""M21 orchestration for deterministic, persisted worktree validation."""

from __future__ import annotations

import fnmatch
import logging
import sqlite3
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.domain.change_validation import (
    ChangedFile,
    ChangeSet,
    FindingCode,
    FindingSeverity,
    ValidationDecision,
    ValidationFinding,
)
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.workspaces import WorkspaceError
from syntra_build.infrastructure.change_validation import (
    ChangeCollector,
    RegexSecretScanner,
)
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence.change_validation import (
    SQLiteValidationRepository,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.workspaces import SQLiteWorkspaceRepository

_LOGGER = logging.getLogger(__name__)
_PROTECTED = (
    ".github/workflows/**",
    ".github/actions/**",
    "deployment/**",
    "scripts/install*",
    "**/release/**",
    "**/signing/**",
    "SECURITY.md",
    "security/**",
)


class ProtectedPathPolicy:
    version = "protected-paths-v1"

    @staticmethod
    def _matches(path: str, pattern: str) -> bool:
        prefix = pattern.removesuffix("/**")
        return fnmatch.fnmatchcase(path, pattern) or (
            pattern.endswith("/**")
            and (path == prefix or path.startswith(prefix + "/"))
        )

    def protected(self, path: str) -> bool:
        return any(self._matches(path, pattern) for pattern in _PROTECTED)

    def authorised(self, path: str, patterns: Iterable[str]) -> bool:
        return any(self._matches(path, pattern) for pattern in patterns)


class ChangeValidationService:
    """Validate actual Git/filesystem evidence without invoking external systems."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        git: TrustedGit,
        data_root: Path,
        *,
        collector: ChangeCollector | None = None,
        scanner: RegexSecretScanner | None = None,
        protected_paths: ProtectedPathPolicy | None = None,
    ) -> None:
        self.connection, self.git = connection, git
        self.workspace_root = (data_root.resolve() / "workspaces").resolve()
        self.workspaces = SQLiteWorkspaceRepository(connection)
        self.records = SQLiteValidationRepository(connection)
        self.collector = collector or ChangeCollector()
        self.scanner = scanner or RegexSecretScanner()
        self.protected_paths = protected_paths or ProtectedPathPolicy()

    def validate(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        correlation_id: str,
        *,
        authorised_protected_paths: Iterable[str] = (),
        now: datetime | None = None,
    ) -> ChangeSet:
        now = now or datetime.now(UTC)
        change_set_id = str(uuid4())
        findings: list[ValidationFinding] = []

        def finding(
            code: FindingCode,
            severity: FindingSeverity,
            message: str,
            remediation: str,
            *,
            path: str | None = None,
            location: str | None = None,
            fingerprint: str | None = None,
            blocking: bool = True,
        ) -> None:
            findings.append(
                ValidationFinding(
                    str(uuid4()),
                    change_set_id,
                    code,
                    severity,
                    path,
                    location,
                    fingerprint,
                    message,
                    remediation,
                    blocking,
                    now,
                )
            )

        managed = self.workspaces.managed_for_project(project_id)
        workspace = self.workspaces.workspace_for_milestone(milestone_id)
        if managed is None or workspace is None or workspace.project_id != project_id:
            raise WorkspaceError("managed workspace does not exist")
        expected_path = (
            self.workspace_root / str(project_id) / str(milestone_id)
        ).resolve(strict=False)
        identity_problem = False
        history_problem = False
        collected = None
        try:
            actual_path = workspace.path.resolve(strict=True)
            identity_problem = (
                workspace.path != expected_path
                or actual_path != expected_path
                or self.workspace_root not in actual_path.parents
                or not self.git.registered(managed.path, actual_path)
                or self.git.origin(actual_path) != managed.remote_url
                or self.git.common_dir(actual_path) != managed.path.resolve(strict=True)
                or not self.git.is_bare(managed.path)
            )
            branch = self.git.branch(actual_path)
            head = self.git.head(actual_path)
            expected_head = workspace.current_head_sha or workspace.base_sha
            history_problem = branch != workspace.branch_name or head != expected_head
        except OSError, WorkspaceError:
            actual_path = workspace.path
            branch, head = (
                workspace.branch_name,
                workspace.current_head_sha or workspace.base_sha,
            )
            identity_problem = True
        if identity_problem:
            finding(
                FindingCode.REPOSITORY_IDENTITY_MISMATCH,
                FindingSeverity.CRITICAL,
                "Persisted repository/workspace identity does not match Git evidence.",
                "Preserve the workspace and require trusted operator reconciliation.",
            )
        if history_problem:
            finding(
                FindingCode.UNEXPECTED_GIT_HISTORY_CHANGE,
                FindingSeverity.CRITICAL,
                "The branch or HEAD differs from persisted trusted history.",
                "Preserve the workspace and investigate the history change.",
            )

        try:
            collected = self.collector.collect(actual_path, workspace.base_sha)
        except WorkspaceError:
            collected = (
                self.collector.collect(workspace.path, workspace.base_sha)
                if not identity_problem
                else None
            )
        if collected is None:
            files: tuple[ChangedFile, ...] = ()
            diff_hash = "sha256:" + "0" * 64
        else:
            files, diff_hash = collected.files, collected.canonical_hash
            for path in (*collected.unsafe_paths, *collected.escaping_symlinks):
                finding(
                    FindingCode.WORKSPACE_ESCAPE_ATTEMPT,
                    FindingSeverity.CRITICAL,
                    "A changed path could escape the assigned workspace.",
                    "Remove the unsafe path and use repository-relative files only.",
                    path=path,
                )
        if not files and not identity_problem:
            finding(
                FindingCode.EMPTY_CHANGE_SET,
                FindingSeverity.WARNING,
                "The implementation produced no effective filesystem change.",
                "Return the existing workspace for implementation rework.",
                blocking=False,
            )
        allowed = tuple(authorised_protected_paths)
        for item in files:
            if self.protected_paths.protected(item.path):
                authorised = self.protected_paths.authorised(item.path, allowed)
                finding(
                    FindingCode.PROTECTED_PATH_CHANGE,
                    FindingSeverity.INFO if authorised else FindingSeverity.HIGH,
                    "A protected path was changed within approved scope."
                    if authorised
                    else "A protected path was changed outside approved scope.",
                    "Retain explicit task authorisation."
                    if authorised
                    else "Remove the change or obtain explicit protected-path scope.",
                    path=item.path,
                    blocking=not authorised,
                )
            if item.status == "DELETED" or item.content_hash is None:
                continue
            candidate = actual_path / item.path
            if candidate.is_symlink():
                continue
            if item.binary:
                finding(
                    FindingCode.BINARY_NOT_SCANNED,
                    FindingSeverity.WARNING,
                    "Binary content was hash-bound but could not be text-scanned.",
                    "Review or explicitly replace binary content before acceptance.",
                    path=item.path,
                    blocking=False,
                )
                continue
            for match in self.scanner.scan(candidate.read_bytes()):
                finding(
                    FindingCode.SECRET_DETECTED,
                    FindingSeverity.HIGH,
                    f"Potential credential detected by {match.rule_id}.",
                    "Replace it with a configuration or environment reference.",
                    path=item.path,
                    location=f"line:{match.line}",
                    fingerprint=match.fingerprint,
                )

        blocked_codes = {
            FindingCode.REPOSITORY_IDENTITY_MISMATCH,
            FindingCode.UNEXPECTED_GIT_HISTORY_CHANGE,
            FindingCode.WORKSPACE_ESCAPE_ATTEMPT,
        }
        if any(item.code in blocked_codes for item in findings):
            decision = ValidationDecision.BLOCKED
        elif any(item.blocking for item in findings) or not files:
            decision = ValidationDecision.REWORK_REQUIRED
        else:
            decision = ValidationDecision.ACCEPT
        result = ChangeSet(
            change_set_id,
            "1.0",
            project_id,
            milestone_id,
            workspace.id,
            workspace.branch_name,
            workspace.base_sha,
            workspace.current_head_sha or workspace.base_sha,
            files,
            diff_hash,
            not files,
            tuple(findings),
            decision,
            correlation_id,
            self.scanner.version,
            self.protected_paths.version,
            now,
        )
        with transaction(self.connection):
            self.records.save(result)
        counts = Counter(
            f"{item.code.value}:{item.severity.value}" for item in findings
        )
        _LOGGER.info(
            "worktree validation completed",
            extra={
                "event": "change_validation_completed",
                "metadata": {
                    "correlation_id": correlation_id,
                    "project_id": str(project_id),
                    "milestone_id": str(milestone_id),
                    "change_set_id": change_set_id,
                    "diff_hash": diff_hash,
                    "finding_counts": dict(counts),
                    "decision": decision.value,
                },
            },
        )
        return result
