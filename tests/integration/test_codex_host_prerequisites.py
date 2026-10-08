"""Offline host prerequisites: no Codex/model or host provisioning side effects."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest


def runtime_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    cli, host, manifest = (
        tmp_path / name for name in ("codex", "tool-host", "manifest")
    )
    cli.write_text(
        '#!/bin/sh\nprintf "%s\\n" "--sandbox --ignore-user-config --config --enable"\n'
    )
    host.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    host.chmod(0o755)
    manifest.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}\n"
            for path in (cli, host)
        )
    )
    script = tmp_path / "check.sh"
    script.write_text(
        Path("scripts/host/check-codex-runtime.sh")
        .read_text()
        .replace("/usr/bin/codex", str(cli))
        .replace("/usr/local/bin/codex-code-mode-host", str(host))
        .replace("/etc/syntra-build/codex-runtime.sha256", str(manifest))
    )
    # Synthetic ownership observation for fixture files; no host chown or sudo.
    bins = tmp_path / "bin"
    bins.mkdir()
    stat = bins / "stat"
    stat.write_text('#!/bin/sh\nif [ "$2" = "%u" ]; then echo 0; else echo 755; fi\n')
    stat.chmod(0o755)
    return script, cli, {**os.environ, "PATH": str(bins) + ":" + os.environ["PATH"]}


@pytest.mark.parametrize(
    "failure", ["missing-host", "changed-host", "incompatible-cli", "valid"]
)
def test_runtime_check_requires_verified_compatible_tool_host(
    tmp_path: Path, failure: str
) -> None:
    script, cli, environment = runtime_fixture(tmp_path)
    if failure == "missing-host":
        (tmp_path / "tool-host").unlink()
    elif failure == "changed-host":
        (tmp_path / "tool-host").write_text("#!/bin/sh\nexit 9\n")
    elif failure == "incompatible-cli":
        cli.write_text("#!/bin/sh\necho legacy\n")
        manifest = tmp_path / "manifest"
        host = tmp_path / "tool-host"
        manifest.write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}\n"
                for path in (cli, host)
            )
        )
    outcome = subprocess.run(
        ["bash", str(script), str(cli)], env=environment, capture_output=True, text=True
    )
    assert outcome.returncode == (0 if failure == "valid" else 66)
    assert not outcome.stdout


@pytest.mark.parametrize("read_only", [False, True])
def test_effective_worker_workspace_permissions_are_checked(
    tmp_path: Path, read_only: bool
) -> None:
    workspace, repository = tmp_path / "workspace", tmp_path / "repo.git"
    workspace.mkdir()
    repository.mkdir()
    (workspace / "SPEC.md").write_text("# approved")
    (workspace / "AGENTS.md").write_text("# approved")
    (repository / "HEAD").write_text("ref: refs/heads/main")
    repository.chmod(0o500)
    (repository / "HEAD").chmod(0o400)
    workspace.chmod(0o500 if read_only else 0o700)
    try:
        result = subprocess.run(
            [
                "bash",
                "scripts/host/check-codex-workspace.sh",
                str(workspace),
                str(repository),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == (66 if read_only else 0)
        assert not list(workspace.glob(".syntra-write-check.*"))
    finally:
        workspace.chmod(0o700)
        repository.chmod(0o700)
