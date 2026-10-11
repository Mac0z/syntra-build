# ruff: noqa: E501
"""Minimal trusted Git operations for the initial approved design/CI baseline."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from syntra_build.application.provisioning import ProvisioningError, ProvisioningFailure
from syntra_build.domain import ProjectId
from syntra_build.infrastructure.config import SecretValue
from syntra_build.infrastructure.git_auth import git_authentication_environment

_BASELINE_FILES = frozenset({"SPEC.md", "AGENTS.md", ".github/workflows/ci.yml"})
_BASELINE_DIRECTORIES = frozenset({".github", ".github/workflows"})
_REMOTE_URL = re.compile(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+\.git")
_GIT_CONFIGURATION = {
    "core.hooksPath": "/dev/null",
    "core.fsmonitor": "false",
    "core.attributesFile": "/dev/null",
    "core.autocrlf": "false",
    "core.filemode": "true",
    "credential.helper": "",
    "protocol.allow": "never",
    "protocol.https.allow": "always",
}


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
        path = self.root / str(project_id)
        if path.is_symlink() or path.resolve().parent != self.root:
            self._reject("invalid provisioning path")
        return path

    @staticmethod
    def _reject(message: str) -> None:
        raise ProvisioningError(ProvisioningFailure.LOCAL_GIT, message)

    @staticmethod
    def _environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
        # Ambient Git settings must not redirect the repository, substitute objects,
        # inject command execution or enable credential helpers.
        environment = {
            key: value
            for key, value in (source if source is not None else os.environ).items()
            if not key.startswith("GIT_")
            and (source is not None or not key.startswith("SYNTRA_GIT_"))
        }
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_CONFIG_COUNT=str(len(_GIT_CONFIGURATION)),
        )
        for index, (key, value) in enumerate(_GIT_CONFIGURATION.items()):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        if source is not None:
            # Only trusted per-operation values may cross the sanitized boundary.
            for key in (
                "GIT_AUTHOR_NAME",
                "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL",
                "GIT_ASKPASS",
            ):
                if key in source:
                    environment[key] = source[key]
        return environment

    @classmethod
    def _run_bytes(
        cls, arguments: list[str], path: Path, env: Mapping[str, str] | None = None
    ) -> bytes:
        try:
            result = subprocess.run(
                arguments,
                cwd=path,
                env=cls._environment(env),
                check=True,
                capture_output=True,
                timeout=120,
            )
        except OSError, subprocess.SubprocessError:
            # Subprocess output may contain credentials; it is not a safe cause.
            raise ProvisioningError(
                ProvisioningFailure.LOCAL_GIT, "trusted Git operation failed"
            ) from None
        return result.stdout

    @classmethod
    def _run(
        cls, arguments: list[str], path: Path, env: Mapping[str, str] | None = None
    ) -> str:
        return cls._run_bytes(arguments, path, env).decode().strip()

    def _validate_worktree(self, path: Path, files: dict[str, bytes] | None) -> None:
        for item in path.rglob("*"):
            name = item.relative_to(path).as_posix()
            if name == ".git" or name.startswith(".git/"):
                continue
            if item.is_symlink():
                self._reject("baseline worktree contains a symlink")
            if item.is_dir() and name in _BASELINE_DIRECTORIES:
                continue
            if name not in _BASELINE_FILES or not stat.S_ISREG(item.stat().st_mode):
                self._reject("unexpected baseline worktree path")
            if files is not None and item.read_bytes() != files[name]:
                self._reject("existing baseline differs")

    def _validate_repository(self, path: Path) -> None:
        git_directory = path / ".git"
        if git_directory.is_symlink() or not git_directory.is_dir():
            self._reject("invalid baseline Git directory")
        for item in git_directory.rglob("*"):
            if item.is_symlink() or not (item.is_dir() or item.is_file()):
                self._reject("invalid baseline Git metadata")
        for name in (
            "objects/info/alternates",
            "info/grafts",
            "info/attributes",
            "shallow",
        ):
            if (git_directory / name).exists():
                self._reject("unexpected baseline Git metadata")
        hooks = git_directory / "hooks"
        if hooks.exists() and any(
            not item.name.endswith(".sample") for item in hooks.iterdir()
        ):
            self._reject("unexpected baseline Git hook")
        configuration = self._run_bytes(
            ["git", "config", "--local", "--no-includes", "--list", "-z"], path
        )
        allowed = {
            b"core.repositoryformatversion": b"0",
            b"core.filemode": b"true",
            b"core.bare": b"false",
            b"core.logallrefupdates": b"true",
            b"remote.origin.fetch": b"+refs/heads/*:refs/remotes/origin/*",
        }
        seen: set[bytes] = set()
        for entry in configuration.split(b"\0"):
            if not entry:
                continue
            key, separator, value = entry.partition(b"\n")
            if not separator or key in seen:
                self._reject("unexpected baseline Git configuration")
            seen.add(key)
            if key == b"remote.origin.url":
                try:
                    remote = value.decode("ascii")
                except UnicodeDecodeError:
                    self._reject("unexpected baseline Git configuration")
                    return
                if _REMOTE_URL.fullmatch(remote) is None:
                    self._reject("unexpected baseline remote")
            elif allowed.get(key) != value:
                self._reject("unexpected baseline Git configuration")
        if self._run(["git", "symbolic-ref", "HEAD"], path) != "refs/heads/main":
            self._reject("unexpected baseline branch")

    def _validate_commit(
        self, path: Path, commit: str, files: dict[str, bytes] | None
    ) -> None:
        if re.fullmatch(r"[a-f0-9]{40}", commit) is None:
            self._reject("invalid baseline SHA")
        headers = self._run_bytes(["git", "cat-file", "commit", commit], path).split(
            b"\n\n", 1
        )[0]
        if any(line.startswith(b"parent ") for line in headers.splitlines()):
            self._reject("unexpected baseline history")
        records = self._run_bytes(
            ["git", "ls-tree", "-rzt", "--full-tree", commit], path
        )
        names: set[str] = set()
        for entry in records.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.split(b" ")
            try:
                filename = name.decode("ascii")
            except UnicodeDecodeError:
                self._reject("unexpected baseline tree")
                return
            if kind == b"tree":
                if mode != b"040000" or filename not in _BASELINE_DIRECTORIES:
                    self._reject("unexpected baseline tree")
                continue
            if mode != b"100644" or kind != b"blob" or filename not in _BASELINE_FILES:
                self._reject("unexpected baseline tree")
            if filename in names:
                self._reject("unexpected baseline tree")
            names.add(filename)
            if (
                files is not None
                and self._run_bytes(
                    ["git", "cat-file", "blob", object_id.decode("ascii")], path
                )
                != files[filename]
            ):
                self._reject("existing baseline differs")
        if names != _BASELINE_FILES:
            self._reject("unexpected baseline tree")
        refs = self._run(
            ["git", "for-each-ref", "--format=%(refname) %(objectname)"], path
        )
        for reference_entry in refs.splitlines():
            reference, object_id_text = reference_entry.split(" ", 1)
            if (
                reference not in {"refs/heads/main", "refs/remotes/origin/main"}
                or object_id_text != commit
            ):
                self._reject("unexpected baseline reference")
        if self._run(["git", "status", "--porcelain", "--untracked-files=all"], path):
            self._reject("baseline worktree differs from commit")

    def create_commit(
        self, project_id: ProjectId, files: dict[str, bytes], message: str
    ) -> str:
        if set(files) != _BASELINE_FILES:
            self._reject("unexpected baseline files")
        path = self._path(project_id)
        path.mkdir(mode=0o700, exist_ok=True)
        self._validate_worktree(path, files)
        git_directory = path / ".git"
        if git_directory.is_symlink():
            self._reject("invalid baseline Git directory")
        if not git_directory.exists():
            self._run(["git", "init", "--initial-branch=main", "--template="], path)
        self._validate_repository(path)
        references = self._run(["git", "for-each-ref", "--format=%(refname)"], path)
        if references:
            committed = self._run(["git", "rev-parse", "HEAD"], path)
            self._validate_commit(path, committed, files)
            return committed
        index = self._run_bytes(["git", "ls-files", "--stage", "-z"], path)
        for entry in index.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ")
            if (
                mode != b"100644"
                or stage != b"0"
                or name not in {item.encode() for item in _BASELINE_FILES}
            ):
                self._reject("unexpected staged baseline files")
            if (
                self._run_bytes(
                    ["git", "cat-file", "blob", object_id.decode("ascii")], path
                )
                != files[name.decode("ascii")]
            ):
                self._reject("existing staged baseline differs")
        for filename, content in files.items():
            target = path / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o644)
        self._run(["git", "add", "--", *sorted(_BASELINE_FILES)], path)
        environment = self._environment()
        environment.update(
            GIT_AUTHOR_NAME="Syntra Build",
            GIT_AUTHOR_EMAIL="syntra@localhost",
            GIT_COMMITTER_NAME="Syntra Build",
            GIT_COMMITTER_EMAIL="syntra@localhost",
        )
        self._run(["git", "commit", "-m", message], path, environment)
        committed = self._run(["git", "rev-parse", "HEAD"], path)
        self._validate_commit(path, committed, files)
        return committed

    def push_main(
        self, project_id: ProjectId, remote_url: str, expected_sha: str
    ) -> None:
        if "@" in remote_url.partition("://")[2].partition("/")[0]:
            self._reject("credential-bearing remote URL rejected")
        if _REMOTE_URL.fullmatch(remote_url) is None:
            self._reject("unexpected baseline remote")
        path = self._path(project_id)
        self._validate_worktree(path, None)
        self._validate_repository(path)
        actual = self._run(["git", "rev-parse", "HEAD"], path)
        if actual != expected_sha:
            self._reject("local baseline SHA differs")
        self._validate_commit(path, expected_sha, None)
        remotes = self._run(["git", "remote"], path).splitlines()
        if "origin" not in remotes:
            self._run(["git", "remote", "add", "origin", remote_url], path)
        elif self._run(["git", "remote", "get-url", "origin"], path) != remote_url:
            self._reject("persisted remote identity differs")
        with self._push_authentication_environment() as environment:
            self._run(
                ["git", "push", "origin", f"{expected_sha}:refs/heads/main"],
                path,
                environment,
            )

    @contextmanager
    def _push_authentication_environment(self) -> Iterator[dict[str, str]]:
        """Supply one Git process with ephemeral username/PAT askpass credentials."""
        if self.github_username is None or self.github_token is None:
            self._reject("GitHub push authentication is unavailable")
        assert self.github_username is not None and self.github_token is not None
        with git_authentication_environment(
            self.root, self.github_username, self.github_token
        ) as environment:
            yield environment
