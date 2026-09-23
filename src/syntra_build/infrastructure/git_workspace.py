"""Constrained subprocess implementation of trusted M19 Git operations."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from pathlib import Path

from syntra_build.domain.change_validation import ChangedFile
from syntra_build.domain.workspaces import AmbiguousPushError, WorkspaceError
from syntra_build.infrastructure.config import SecretValue
from syntra_build.infrastructure.git_auth import git_authentication_environment


class TrustedGit:
    def __init__(
        self,
        authentication_root: Path,
        *,
        username: str | None = None,
        token: SecretValue | None = None,
    ) -> None:
        self.authentication_root = authentication_root.resolve()
        self.username, self.token = username, token

    @staticmethod
    def _run(
        cwd: Path,
        arguments: list[str],
        env: Mapping[str, str] | None = None,
    ) -> str:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                env=dict(env) if env else None,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise WorkspaceError("trusted Git operation failed") from error
        return result.stdout.strip()

    def _auth(self, remote_url: str):  # type: ignore[no-untyped-def]
        if remote_url.startswith("https://"):
            if self.username is None or self.token is None:
                raise WorkspaceError("Git authentication is unavailable")
            return git_authentication_environment(
                self.authentication_root, self.username, self.token
            )
        return nullcontext(dict(os.environ, GIT_TERMINAL_PROMPT="0"))

    def ensure_bare(self, path: Path, remote_url: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self._run(path, ["rev-parse", "--is-bare-repository"]) != "true":
                raise WorkspaceError("managed repository path is not a bare repository")
        else:
            path.mkdir(mode=0o700)
            self._run(path, ["init", "--bare"])
            self._run(path, ["remote", "add", "origin", remote_url])
        if self._run(path, ["remote", "get-url", "origin"]) != remote_url:
            raise WorkspaceError("managed repository remote identity differs")

    def fetch(self, path: Path, remote_url: str, branch: str) -> str:
        with self._auth(remote_url) as environment:
            self._run(
                path,
                [
                    "fetch",
                    "--prune",
                    "origin",
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                ],
                environment,
            )
        return self._run(path, ["rev-parse", f"refs/remotes/origin/{branch}"])

    def branch_sha(self, repository: Path, branch: str) -> str | None:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def has_commit(self, repository: Path, sha: str) -> bool:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0

    def fetch_branch(self, repository: Path, remote_url: str, branch: str) -> str:
        return self.fetch(repository, remote_url, branch)

    def create_branch(self, repository: Path, branch: str, base_sha: str) -> None:
        existing = self.branch_sha(repository, branch)
        if existing is None:
            self._run(repository, ["branch", branch, base_sha])
        elif existing != base_sha:
            raise WorkspaceError("existing milestone branch has unexpected history")

    def add_worktree(self, repository: Path, path: Path, branch: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(repository, ["worktree", "add", str(path), branch])

    def registered(self, repository: Path, path: Path) -> bool:
        records = self._run(repository, ["worktree", "list", "--porcelain"])
        return f"worktree {path}" in records.splitlines()

    def origin(self, worktree: Path) -> str:
        return self._run(worktree, ["remote", "get-url", "origin"])

    def branch(self, worktree: Path) -> str:
        return self._run(worktree, ["branch", "--show-current"])

    def head(self, worktree: Path) -> str:
        return self._run(worktree, ["rev-parse", "HEAD"])

    def common_dir(self, worktree: Path) -> Path:
        value = self._run(worktree, ["rev-parse", "--git-common-dir"])
        return (worktree / value).resolve(strict=True)

    def is_bare(self, repository: Path) -> bool:
        return self._run(repository, ["rev-parse", "--is-bare-repository"]) == "true"

    def changes(
        self, worktree: Path
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        raw = self._run(worktree, ["status", "--porcelain=v1", "-z"])
        tracked: list[str] = []
        staged: list[str] = []
        untracked: list[str] = []
        for record in raw.split("\0"):
            if not record:
                continue
            code, name = record[:2], record[3:]
            if code == "??":
                untracked.append(name)
            else:
                if code[0] != " ":
                    staged.append(name)
                if code[1] != " ":
                    tracked.append(name)
        return tuple(tracked), tuple(staged), tuple(untracked)

    def commit(
        self, worktree: Path, paths: Iterable[str], message: str
    ) -> tuple[str, str]:
        self.stage(worktree, paths)
        return self.commit_staged(worktree, message)

    def stage(self, worktree: Path, paths: Iterable[str]) -> None:
        selected = tuple(paths)
        if not selected or any(
            not item or Path(item).is_absolute() or ".." in Path(item).parts
            for item in selected
        ):
            raise WorkspaceError("explicit safe staging paths are required")
        self._run(worktree, ["add", "--", *selected])

    def index_changes(
        self, worktree: Path, trusted_head: str
    ) -> tuple[ChangedFile, ...]:
        """Describe the exact index tree that a subsequent commit would consume."""
        names = tuple(
            item
            for item in self._run(
                worktree, ["diff", "--cached", "--name-only", "-z", trusted_head]
            ).split("\0")
            if item
        )
        result: list[ChangedFile] = []
        for name in sorted(names, key=os.fsencode):
            raw = self._run(worktree, ["ls-files", "-s", "--", name])
            if not raw:
                result.append(
                    ChangedFile(name, "DELETED", True, False, None, "deleted", None)
                )
                continue
            metadata = raw.split("\t", 1)[0].split()
            mode, blob = metadata[0], metadata[1]
            payload = subprocess.run(
                ["git", "cat-file", "blob", blob],
                cwd=worktree,
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
            base_exists = (
                subprocess.run(
                    ["git", "cat-file", "-e", f"{trusted_head}:{name}"],
                    cwd=worktree,
                    check=False,
                    capture_output=True,
                    timeout=30,
                ).returncode
                == 0
            )
            result.append(
                ChangedFile(
                    name,
                    "MODIFIED" if base_exists else "ADDED",
                    True,
                    b"\0" in payload[:8192],
                    hashlib.sha256(payload).hexdigest(),
                    "symlink" if mode == "120000" else "file",
                    mode,
                )
            )
        return tuple(result)

    def commit_staged(self, worktree: Path, message: str) -> tuple[str, str]:
        parent = self.head(worktree)
        environment = dict(os.environ)
        environment.update(
            GIT_AUTHOR_NAME="Syntra Build",
            GIT_AUTHOR_EMAIL="syntra@localhost",
            GIT_COMMITTER_NAME="Syntra Build",
            GIT_COMMITTER_EMAIL="syntra@localhost",
        )
        self._run(worktree, ["commit", "-m", message], environment)
        return self.head(worktree), parent

    def remote_branch_sha(
        self,
        repository: Path,
        remote_url: str,
        branch: str,
    ) -> str | None:
        with self._auth(remote_url) as environment:
            output = self._run(
                repository,
                ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
                environment,
            )
        return output.split()[0] if output else None

    def push(self, repository: Path, remote_url: str, branch: str) -> None:
        with self._auth(remote_url) as environment:
            try:
                subprocess.run(
                    [
                        "git",
                        "push",
                        "origin",
                        f"refs/heads/{branch}:refs/heads/{branch}",
                    ],
                    cwd=repository,
                    env=dict(environment),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except OSError, subprocess.SubprocessError:
                # Git cannot prove whether the server accepted a request before a
                # transport failure. Deliberately discard stderr and its cause.
                raise AmbiguousPushError(
                    "Git push outcome requires reconciliation"
                ) from None

    def remove_worktree(self, repository: Path, path: Path) -> None:
        self._run(repository, ["worktree", "remove", str(path)])

    def prune_worktrees(self, repository: Path) -> None:
        self._run(repository, ["worktree", "prune"])
