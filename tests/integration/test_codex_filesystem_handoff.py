"""Real Linux host boundary; run as root only in a disposable test host."""

from __future__ import annotations

import os
import pwd
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or shutil.which("setfacl") is None,
    reason="requires a disposable Linux root host with the acl package",
)
ROOT = Path(__file__).resolve().parents[2]


def run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        capture_output=True,
        text=True,
        timeout=15,
        cwd="/tmp",
    )


@pytest.fixture(scope="module")
def identities() -> Iterator[None]:
    created = []
    for user in ("syntra-build", "syntra-codex"):
        try:
            pwd.getpwnam(user)
        except KeyError:
            subprocess.run(
                ["useradd", "--system", "--user-group", "--home-dir", "/tmp", user],
                check=True,
            )
            created.append(user)
    yield
    for user in reversed(created):
        subprocess.run(["userdel", user], check=True)


@pytest.fixture
def host(identities: None) -> Iterator[tuple[Path, Path, Path, Path]]:
    del identities
    with tempfile.TemporaryDirectory(prefix="syntra-handoff-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        data = root / "data"
        project, milestone = str(uuid4()), str(uuid4())
        workspace = data / "workspaces" / project / milestone
        repository = data / "repositories" / project / "repo.git"
        private = data / "private"
        for path in (workspace, repository, private):
            path.mkdir(parents=True)
        seed = root / "seed"
        assert run("git", "init", "-q", seed).returncode == 0
        for name in ("SPEC.md", "AGENTS.md"):
            (seed / name).write_text("approved\n")
        assert run("git", "-C", seed, "add", ".").returncode == 0
        assert (
            run(
                "git",
                "-C",
                seed,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ).returncode
            == 0
        )
        assert run("git", "clone", "-q", "--bare", seed, repository).returncode == 0
        assert (
            run(
                "git",
                f"--git-dir={repository}",
                "worktree",
                "add",
                "-b",
                "milestone",
                workspace,
            ).returncode
            == 0
        )
        for path in (data, *data.rglob("*")):
            shutil.chown(path, "syntra-build", "syntra-build")
            path.chmod(0o700 if path.is_dir() else 0o600)
        (private / "state.db").write_text("control-plane-only\n")
        for path in (*workspace.iterdir(), repository / "HEAD", private / "state.db"):
            shutil.chown(path, "syntra-build", "syntra-build")
            path.chmod(0o600)
        runtime = root / "check-runtime"
        runtime.write_text("#!/bin/sh\nexit 0\n")
        runtime.chmod(0o755)
        probe = root / "check-workspace"
        shutil.copyfile(ROOT / "scripts/host/check-codex-workspace.sh", probe)
        probe.chmod(0o755)
        acl = root / "acl.py"
        acl.write_text(
            (ROOT / "scripts/host/syntra-codex-acl.py")
            .read_text()
            .replace("/var/lib/syntra-build/workspaces", str(data / "workspaces"))
        )
        cli = root / "codex"
        cli.write_text(
            "#!/bin/sh\numask 077\nmkdir -p src/nested tests\n"
            "printf generated > src/nested/code.py\nprintf test > tests/test.py\n"
            "printf ignore > .gitignore\nchmod 700 src src/nested tests\n"
            "chmod 600 src/nested/code.py tests/test.py .gitignore\n"
            f"test ! -r '{private}/state.db' || exit 91\n"
            f"test ! -w '{repository}/HEAD' || exit 92\n"
            "test ! -w .git || exit 93\nexit 0\n"
        )
        cli.chmod(0o755)
        launcher = root / "launch"
        launcher.write_text(
            (ROOT / "scripts/host/syntra-codex-launch")
            .read_text()
            .replace("/var/lib/syntra-build", str(data))
            .replace("/usr/local/libexec/check-codex-runtime", str(runtime))
            .replace("/usr/local/libexec/check-codex-workspace", str(probe))
            .replace("/usr/local/libexec/syntra-codex-acl.py", str(acl))
            .replace("/usr/local/bin/codex", str(cli))
            .replace("/run/lock/syntra-codex.lock", str(root / "lock"))
        )
        yield launcher, workspace, cli, repository


def as_user(user: str, *args: str | Path) -> subprocess.CompletedProcess[str]:
    return run("setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups", *args)


def assert_handoff(workspace: Path, repository: Path) -> None:
    for relative in (".gitignore", "src/nested/code.py", "tests/test.py"):
        assert as_user("syntra-build", "cat", workspace / relative).returncode == 0
    assert as_user("syntra-build", "find", workspace / "src").returncode == 0
    status = as_user("syntra-build", "git", "-C", workspace, "status", "--porcelain")
    assert status.returncode == 0, status.stderr
    assert "?? .gitignore" in status.stdout and "?? src/" in status.stdout
    assert (
        as_user("syntra-codex", "cat", workspace / "src/nested/code.py").returncode != 0
    )
    assert as_user("syntra-codex", "test", "-w", repository / "HEAD").returncode != 0
    assert as_user("syntra-codex", "cat", repository / "HEAD").returncode != 0


@pytest.mark.parametrize("exit_code", [0, 7])
def test_real_restrictive_worker_output_is_handed_off_on_all_exits(
    host: tuple[Path, Path, Path, Path], exit_code: int
) -> None:
    launcher, workspace, cli, repository = host
    cli.write_text(cli.read_text().replace("exit 0\n", f"exit {exit_code}\n"))
    metadata = (repository / "HEAD").read_bytes()
    result = run("bash", launcher, workspace, cli, "exec", "-")
    assert result.returncode == exit_code, result.stderr
    assert (workspace / "src/nested/code.py").stat().st_uid == pwd.getpwnam(
        "syntra-codex"
    ).pw_uid
    assert_handoff(workspace, repository)
    assert (repository / "HEAD").read_bytes() == metadata
    assert stat_mode(repository / "HEAD") == 0o600
    assert as_user("syntra-codex", "test", "-w", workspace / ".git").returncode != 0


def test_orphan_repository_grants_are_revoked_before_another_invocation(
    host: tuple[Path, Path, Path, Path],
) -> None:
    launcher, workspace, cli, repository = host
    repository_root = repository.parent.parent
    orphan = repository_root / str(uuid4()) / "repo.git"
    orphan.mkdir(parents=True)
    (orphan / "HEAD").write_text("orphan metadata\n")
    for path in (orphan.parent, orphan, orphan / "HEAD"):
        shutil.chown(path, "syntra-build", "syntra-build")
        path.chmod(0o700 if path.is_dir() else 0o600)
    assert (
        run(
            "setfacl", "-m", "u:syntra-codex:x", repository_root, orphan.parent
        ).returncode
        == 0
    )
    assert run("setfacl", "-Rm", "u:syntra-codex:rX", orphan).returncode == 0
    cli.write_text(
        cli.read_text().replace(
            "exit 0\n", f"test ! -r '{orphan}/HEAD' || exit 94\nexit 0\n"
        )
    )
    result = run("bash", launcher, workspace, cli, "exec", "-")
    assert result.returncode == 0, result.stderr
    assert as_user("syntra-codex", "cat", orphan / "HEAD").returncode != 0
    assert_handoff(workspace, repository)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.parametrize("interruption", [signal.SIGTERM, signal.SIGKILL])
def test_signal_and_repeated_cleanup_preserve_generated_files(
    host: tuple[Path, Path, Path, Path],
    interruption: signal.Signals,
) -> None:
    launcher, workspace, cli, repository = host
    cli.write_text(cli.read_text().replace("exit 0\n", "touch ready\nsleep 30\n"))
    process = subprocess.Popen(
        ["bash", str(launcher), str(workspace), str(cli), "exec", "-"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (workspace / "ready").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        os.killpg(process.pid, interruption)
        assert process.wait(timeout=10) == (
            143 if interruption == signal.SIGTERM else -9
        )
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    for _ in range(2):
        result = run("bash", launcher, "--cleanup", workspace)
        assert result.returncode == 0, result.stderr
        assert_handoff(workspace, repository)


@pytest.mark.parametrize("failure", ["escape", "git-symlink", "hardlink", "acl"])
def test_unsafe_paths_and_acl_failures_fail_closed(
    host: tuple[Path, Path, Path, Path], failure: str
) -> None:
    launcher, workspace, cli, repository = host
    assert run("bash", launcher, workspace, cli, "exec", "-").returncode == 0
    outside = launcher.parent / "outside"
    outside.write_text("unchanged\n")
    outside.chmod(0o600)
    before = run("getfacl", "-cp", outside).stdout
    if failure == "escape":
        (workspace / "escape").symlink_to(outside)
    elif failure == "git-symlink":
        (workspace / ".git").unlink()
        (workspace / ".git").symlink_to(outside)
    elif failure == "hardlink":
        os.link(outside, workspace / "hardlink")
    else:
        acl = launcher.parent / "acl.py"
        acl.write_text(acl.read_text().replace("/usr/bin/setfacl", "/bin/false"))
    result = run("bash", launcher, "--repair-handoff", workspace)
    assert result.returncode != 0
    assert run("getfacl", "-cp", outside).stdout == before
    assert outside.read_text() == "unchanged\n"
    assert as_user("syntra-codex", "test", "-x", workspace).returncode != 0
    assert as_user("syntra-codex", "test", "-w", repository / "HEAD").returncode != 0


def test_cleanup_repairs_explicitly_masked_acl(
    host: tuple[Path, Path, Path, Path],
) -> None:
    launcher, workspace, cli, repository = host
    assert run("bash", launcher, workspace, cli, "exec", "-").returncode == 0
    for path in (
        workspace / "src",
        workspace / "src/nested",
        workspace / "src/nested/code.py",
    ):
        assert run("setfacl", "-m", "u:syntra-build:r-x,m::---", path).returncode == 0
    assert (
        as_user("syntra-build", "cat", workspace / "src/nested/code.py").returncode != 0
    )
    assert run("bash", launcher, "--cleanup", workspace).returncode == 0
    assert_handoff(workspace, repository)
