# ruff: noqa: E501
"""Minimal trusted Git operations for the M18 initial design baseline only."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from syntra_build.application.provisioning import ProvisioningError, ProvisioningFailure
from syntra_build.domain import ProjectId
from syntra_build.infrastructure.config import SecretValue
from syntra_build.infrastructure.git_auth import git_authentication_environment


class SubprocessInitialBaselineGit:
    """Create one controlled repository; this is intentionally not an M19 workspace API."""

    def __init__(
        self,
        root: Path,
        *,
        github_username: str | None = None,
        github_token: SecretValue | None = None,
    ) -> None:
        self.root = root.resolve()
        self.github_username = github_username
        self.github_token = github_token
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: ProjectId) -> Path:
        path = (self.root / str(project_id)).resolve()
        if path.parent != self.root:
            raise ProvisioningError(
                ProvisioningFailure.LOCAL_GIT, "invalid provisioning path"
            )
        return path

    @staticmethod
    def _run(
        arguments: list[str], path: Path, env: Mapping[str, str] | None = None
    ) -> str:
        try:
            result = subprocess.run(
                arguments,
                cwd=path,
                env=dict(env) if env else None,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ProvisioningError(
                ProvisioningFailure.LOCAL_GIT, "trusted Git operation failed"
            ) from error
        return result.stdout.strip()

    def create_commit(
        self, project_id: ProjectId, files: dict[str, bytes], message: str
    ) -> str:
        path = self._path(project_id)
        path.mkdir(mode=0o700, exist_ok=True)
        if set(files) != {"SPEC.md", "AGENTS.md"}:
            raise ProvisioningError(
                ProvisioningFailure.LOCAL_GIT, "unexpected baseline files"
            )
        existing_repository = (path / ".git").exists()
        if not existing_repository:
            self._run(["git", "init", "--initial-branch=main"], path)
        for name, content in files.items():
            (path / name).write_bytes(content)
        self._run(["git", "add", "--", "SPEC.md", "AGENTS.md"], path)
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Syntra Build",
                "GIT_AUTHOR_EMAIL": "syntra@localhost",
                "GIT_COMMITTER_NAME": "Syntra Build",
                "GIT_COMMITTER_EMAIL": "syntra@localhost",
            }
        )
        if existing_repository:
            head = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=path,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if head.returncode == 0:
                committed = head.stdout.strip()
                for name, content in files.items():
                    if self._run(
                        ["git", "show", f"{committed}:{name}"], path
                    ).encode() != content.rstrip(b"\n"):
                        raise ProvisioningError(
                            ProvisioningFailure.LOCAL_GIT, "existing baseline differs"
                        )
                return committed
        self._run(["git", "commit", "-m", message], path, environment)
        return self._run(["git", "rev-parse", "HEAD"], path, environment)

    def push_main(
        self, project_id: ProjectId, remote_url: str, expected_sha: str
    ) -> None:
        if "@" in remote_url.partition("://")[2].partition("/")[0]:
            raise ProvisioningError(
                ProvisioningFailure.LOCAL_GIT, "credential-bearing remote URL rejected"
            )
        path = self._path(project_id)
        actual = self._run(["git", "rev-parse", "HEAD"], path)
        if actual != expected_sha:
            raise ProvisioningError(
                ProvisioningFailure.LOCAL_GIT, "local baseline SHA differs"
            )
        remotes = self._run(["git", "remote"], path).splitlines()
        if "origin" not in remotes:
            self._run(["git", "remote", "add", "origin", remote_url], path)
        elif self._run(["git", "remote", "get-url", "origin"], path) != remote_url:
            raise ProvisioningError(
                ProvisioningFailure.LOCAL_GIT, "persisted remote identity differs"
            )
        with self._push_authentication_environment() as environment:
            self._run(["git", "push", "origin", "main:main"], path, environment)

    @contextmanager
    def _push_authentication_environment(self) -> Iterator[dict[str, str]]:
        """Supply one Git process with ephemeral username/PAT askpass credentials."""
        if self.github_username is None or self.github_token is None:
            raise ProvisioningError(
                ProvisioningFailure.LOCAL_GIT,
                "GitHub push authentication is unavailable",
            )
        with git_authentication_environment(
            self.root, self.github_username, self.github_token
        ) as environment:
            yield environment
