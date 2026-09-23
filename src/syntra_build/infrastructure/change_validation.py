"""Deterministic Git change collection and offline secret scanning."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from syntra_build.domain.change_validation import ChangedFile
from syntra_build.domain.workspaces import WorkspaceError


@dataclass(frozen=True, slots=True)
class CollectedChanges:
    files: tuple[ChangedFile, ...]
    canonical_hash: str
    unsafe_paths: tuple[str, ...]
    escaping_symlinks: tuple[str, ...]
    scan_payloads: Mapping[str, bytes]
    scan_omissions: tuple[str, ...]


_MAX_SCAN_FILE_BYTES = 2 * 1024 * 1024
_MAX_SCAN_TOTAL_BYTES = 8 * 1024 * 1024


class ChangeCollector:
    """Collect the effective tree, including index and untracked state."""

    @staticmethod
    def _git(worktree: Path, *args: str) -> bytes:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=worktree,
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise WorkspaceError("change collection failed") from error

    @staticmethod
    def _paths(raw: bytes) -> set[str]:
        return {
            item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item
        }

    @staticmethod
    def _safe(path: str) -> bool:
        parsed = PurePosixPath(path)
        return bool(path) and not parsed.is_absolute() and ".." not in parsed.parts

    def collect(self, worktree: Path, base_sha: str) -> CollectedChanges:
        root = worktree.resolve(strict=True)
        base_records = self._git(root, "ls-tree", "-r", "-z", base_sha).split(b"\0")
        base: dict[str, tuple[str, str]] = {}
        for record in base_records:
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            tree_mode, _kind, blob = metadata.decode().split()
            base[raw_path.decode("utf-8", "surrogateescape")] = (tree_mode, blob)
        current = self._paths(
            self._git(
                root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            )
        )
        staged = self._paths(self._git(root, "diff", "--cached", "--name-only", "-z"))
        index: dict[str, tuple[str, str]] = {}
        for record in self._git(root, "ls-files", "-s", "-z").split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            index_mode, index_blob, stage = metadata.decode().split()
            if stage == "0":
                index[raw_path.decode("utf-8", "surrogateescape")] = (
                    index_mode,
                    index_blob,
                )
        unsafe: list[str] = []
        escapes: list[str] = []
        files: list[ChangedFile] = []
        canonical: list[dict[str, object]] = []
        scan_payloads: dict[str, bytes] = {}
        scan_omissions: list[str] = []
        retained_scan_bytes = 0
        for name in sorted(set(base) | current, key=os.fsencode):
            if not self._safe(name):
                unsafe.append(name)
                continue
            path = root / name
            before = base.get(name)
            exists = path.exists() or path.is_symlink()
            kind: str = "deleted"
            digest: str | None = None
            binary = False
            mode: int | None = None
            if exists:
                stat = path.lstat()
                mode = stat.st_mode & 0o777
                if path.is_symlink():
                    kind = "symlink"
                    target = os.readlink(path)
                    payload = os.fsencode(target)
                    resolved = (path.parent / target).resolve(strict=False)
                    if resolved != root and root not in resolved.parents:
                        escapes.append(name)
                elif path.is_file():
                    kind = "file"
                    payload = path.read_bytes()
                    binary = b"\0" in payload[:8192]
                else:
                    kind, payload = "other", b""
                    escapes.append(name)
                digest = hashlib.sha256(payload).hexdigest()
            base_digest = before[1] if before else None
            base_mode = before[0] if before else None
            # Git blob IDs differ from SHA-256 content hashes, so compare the
            # actual base bytes for an exact effective-tree decision.
            unchanged = False
            if before and exists and kind in {"file", "symlink"}:
                prior = self._git(root, "show", f"{base_sha}:{name}")
                unchanged = hashlib.sha256(prior).hexdigest() == digest
                current_git_mode = (
                    "120000"
                    if kind == "symlink"
                    else "100755"
                    if mode is not None and mode & 0o111
                    else "100644"
                )
                unchanged = unchanged and (current_git_mode == base_mode)
            if before and not exists:
                status = "DELETED"
            elif not before and exists:
                status = "ADDED"
            elif not unchanged or name in staged:
                status = "MODIFIED"
            else:
                continue
            git_mode = (
                None
                if not exists
                else "120000"
                if kind == "symlink"
                else "100755"
                if mode is not None and mode & 0o111
                else "100644"
            )
            item = ChangedFile(
                name, status, name in staged, binary, digest, kind, git_mode
            )
            files.append(item)
            if kind == "file" and not binary and status != "DELETED":
                if (
                    len(payload) > _MAX_SCAN_FILE_BYTES
                    or retained_scan_bytes + len(payload) > _MAX_SCAN_TOTAL_BYTES
                ):
                    scan_omissions.append(name)
                else:
                    # This is the exact object used for content hashing above.
                    # It is retained only for this validation call.
                    scan_payloads[name] = payload
                    retained_scan_bytes += len(payload)
            canonical.append(
                {
                    "path": name,
                    "status": status,
                    "staged": name in staged,
                    "kind": kind,
                    "mode": mode,
                    "content_sha256": digest,
                    "base_blob": base_digest,
                    "base_mode": base_mode,
                    "index_blob": index.get(name, (None, None))[1],
                    "index_mode": index.get(name, (None, None))[0],
                }
            )
        encoded = json.dumps(
            canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        return CollectedChanges(
            tuple(files),
            f"sha256:{hashlib.sha256(encoded).hexdigest()}",
            tuple(unsafe),
            tuple(escapes),
            MappingProxyType(scan_payloads),
            tuple(scan_omissions),
        )


@dataclass(frozen=True, slots=True)
class SecretMatch:
    rule_id: str
    line: int
    fingerprint: str


class RegexSecretScanner:
    """Small conservative scanner behind a replaceable offline interface."""

    version = "regex-v1"
    _RULES = (
        (
            "GITHUB_TOKEN",
            re.compile(
                r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
            ),
        ),
        (
            "PRIVATE_KEY",
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        ),
        ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
        (
            "CONNECTION_CREDENTIAL",
            re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/]+:[^\s/@]+@", re.I),
        ),
        (
            "SECRET_ASSIGNMENT",
            re.compile(
                r"(?i)\b(?:password|api[_-]?key|access[_-]?token|client[_-]?secret|signing[_-]?secret)\s*[:=]\s*['\"]?([A-Za-z0-9_./+\-=]{12,})"
            ),
        ),
    )

    def scan(self, data: bytes) -> tuple[SecretMatch, ...]:
        text = data.decode("utf-8", "replace")
        found: list[SecretMatch] = []
        for number, line in enumerate(text.splitlines(), 1):
            for rule, pattern in self._RULES:
                for match in pattern.finditer(line):
                    value = match.group(0)
                    fingerprint = hashlib.sha256(value.encode()).hexdigest()[:12]
                    found.append(SecretMatch(rule, number, f"sha256:{fingerprint}"))
        return tuple(found)
