"""Real Linux host boundary; run as root only in a disposable test host."""

from __future__ import annotations

import gc
import os
import pwd
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from handoff_venv import populated_venv

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
    return run(
        "setpriv",
        f"--reuid={user}",
        f"--regid={user}",
        "--init-groups",
        "env",
        "-i",
        "PATH=/usr/bin:/bin",
        f"HOME={pwd.getpwnam(user).pw_dir}",
        "XDG_CONFIG_HOME=/nonexistent",
        "GIT_CONFIG_NOSYSTEM=1",
        *args,
    )


def assert_handoff(workspace: Path, repository: Path) -> None:
    for relative in (".gitignore", "src/nested/code.py", "tests/test.py"):
        assert as_user("syntra-build", "cat", workspace / relative).returncode == 0
    assert as_user("syntra-build", "find", workspace / "src").returncode == 0
    status = as_user("syntra-build", "git", "-C", workspace, "status", "--porcelain")
    assert status.returncode == 0, status.stderr
    assert not status.stderr
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


@pytest.mark.parametrize(
    "failure",
    [
        "escape",
        "git-symlink",
        "hardlink",
        "acl",
        "default-acl",
        "fifo",
        "ignored-fifo",
        "loop",
        "dangling-escape",
    ],
)
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
    elif failure == "loop":
        (workspace / "escape").symlink_to("escape")
    elif failure == "dangling-escape":
        (workspace / "escape").symlink_to("missing/../../outside")
    elif failure == "git-symlink":
        (workspace / ".git").unlink()
        (workspace / ".git").symlink_to(outside)
    elif failure == "hardlink":
        os.link(outside, workspace / "hardlink")
    elif failure == "fifo":
        os.mkfifo(workspace / "pipe")
    elif failure == "ignored-fifo":
        (workspace / ".venv").mkdir()
        (workspace / ".gitignore").write_text(".venv/\n")
        os.mkfifo(workspace / ".venv/pipe")
    else:
        if failure == "acl":
            (workspace / "src/nested/code.py").chmod(0o600)
        else:
            assert run("setfacl", "-m", "d:m::---", workspace / "src").returncode == 0
        acl = launcher.parent / "acl.py"
        call = (
            "self.lib.acl_set_fd(fd, handle)"
            if failure == "acl"
            else "self.lib.acl_set_file(\n"
            '                    os.fsencode(f"/proc/self/fd/{fd}"), '
            "self.DEFAULT, handle\n"
            "                )"
        )
        assert call in acl.read_text()
        acl.write_text(acl.read_text().replace(call, "-1"))
    result = run("bash", launcher, "--repair-handoff", workspace)
    assert result.returncode != 0
    assert run("getfacl", "-cp", outside).stdout == before
    assert outside.read_text() == "unchanged\n"
    assert as_user("syntra-codex", "test", "-x", workspace).returncode != 0
    assert as_user("syntra-codex", "test", "-w", repository / "HEAD").returncode != 0


@pytest.mark.parametrize("target", ["src/nested/code.py", "missing/../other"])
def test_internal_source_links_are_opaque_but_allowed(
    host: tuple[Path, Path, Path, Path], target: str
) -> None:
    launcher, workspace, cli, repository = host
    assert run("bash", launcher, workspace, cli, "exec", "-").returncode == 0
    (workspace / "source-link").symlink_to(target)
    assert run("bash", launcher, "--cleanup", workspace).returncode == 0
    assert_handoff(workspace, repository)
    assert (workspace / "source-link").is_symlink()
    if target.startswith("src/"):
        assert (
            as_user("syntra-build", "cat", workspace / "source-link").stdout
            == "generated"
        )


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


def restrictive_worker_tree(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if not path.is_symlink():
            shutil.chown(path, "syntra-codex", "syntra-codex")
            path.chmod(0o700 if path.is_dir() else 0o600)


@pytest.fixture
def venv_host(
    host: tuple[Path, Path, Path, Path],
) -> tuple[Path, Path, Path, Path]:
    launcher, workspace, cli, _repository = host
    environment, _count = populated_venv(workspace)
    restrictive_worker_tree(environment)
    cli.write_text(
        cli.read_text().replace(
            "printf ignore > .gitignore", "printf '.venv/\\nignore\\n' > .gitignore"
        )
    )
    return host


@pytest.mark.parametrize("exit_code", [0, 7])
def test_actual_ignored_venv_handoff_on_process_exits(
    venv_host: tuple[Path, Path, Path, Path], exit_code: int
) -> None:
    launcher, workspace, cli, repository = venv_host
    if exit_code != 0:
        # Also exercise Python 3.14's own .venv/.gitignore, without a root rule.
        (workspace / ".gitignore").write_text("ignore\n")
        cli.write_text(cli.read_text().replace(".venv/\\nignore", "ignore"))
    cli.write_text(cli.read_text().replace("exit 0\n", f"exit {exit_code}\n"))
    interpreter = (workspace / ".venv/bin/python").resolve()
    before = run("getfacl", "-cp", interpreter).stdout
    result = run("bash", launcher, workspace, cli, "exec", "-")
    assert result.returncode == exit_code, result.stderr
    assert_handoff(workspace, repository)
    assert run("getfacl", "-cp", interpreter).stdout == before
    assert (
        as_user("syntra-build", "cat", workspace / ".venv/.gitignore").returncode == 0
    )
    assert as_user("syntra-codex", "test", "-x", workspace).returncode != 0


@pytest.mark.parametrize("reconcile", ["cleanup", "restart"])
def test_interrupted_venv_handoff_and_restart_reconciliation(
    venv_host: tuple[Path, Path, Path, Path], reconcile: str
) -> None:
    launcher, workspace, cli, repository = venv_host
    cli.write_text(cli.read_text().replace("exit 0\n", "touch ready\nsleep 30\n"))
    process = subprocess.Popen(
        ["bash", str(launcher), str(workspace), str(cli), "exec", "-"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not (workspace / "ready").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        os.killpg(process.pid, signal.SIGKILL)
        assert process.wait(timeout=10) == -9
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    assert (
        as_user("syntra-build", "cat", workspace / "src/nested/code.py").returncode != 0
    )
    original = (workspace / "src/nested/code.py").read_bytes()
    command: tuple[str | Path, ...]
    if reconcile == "cleanup":
        command = ("bash", launcher, "--cleanup", workspace)
    else:
        # A restart launch must reconcile old output without regenerating it.
        cli.write_text("#!/bin/sh\nexit 7\n")
        command = ("bash", launcher, workspace, cli, "exec", "-")
    result = run(*command)
    assert result.returncode == (0 if reconcile == "cleanup" else 7), result.stderr
    assert_handoff(workspace, repository)
    assert (workspace / "src/nested/code.py").read_bytes() == original
    assert run("bash", launcher, "--cleanup", workspace).returncode == 0


@pytest.mark.parametrize(
    "source", ["untracked", "force-tracked", "ancestor", "ignored-target"]
)
def test_ignored_environment_does_not_hide_unsafe_source_links(
    venv_host: tuple[Path, Path, Path, Path], source: str
) -> None:
    launcher, workspace, cli, _repository = venv_host
    assert run("bash", launcher, workspace, cli, "exec", "-").returncode == 0
    target = (workspace / ".venv/bin/python").resolve()
    before = run("getfacl", "-cp", target).stdout
    if source == "untracked":
        (workspace / "source-python").symlink_to(target)
    elif source == "ignored-target":
        (workspace / "source-python").symlink_to(".venv/bin/python")
    elif source == "force-tracked":
        assert (
            as_user(
                "syntra-build", "git", "-C", workspace, "add", "-f", ".venv/bin/python"
            ).returncode
            == 0
        )
    else:
        blob = as_user(
            "syntra-build", "git", "-C", workspace, "rev-parse", "HEAD:SPEC.md"
        )
        assert blob.returncode == 0
        assert (
            as_user(
                "syntra-build",
                "git",
                "-C",
                workspace,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob.stdout.strip()},src/cached.py",
            ).returncode
            == 0
        )
        shutil.rmtree(workspace / "src")
        (workspace / "src").symlink_to(target.parent, target_is_directory=True)
        # An ignored ancestor cannot hide its previously cached descendant.
        (workspace / ".gitignore").write_text(".venv/\nsrc/\n")
    assert run("bash", launcher, "--repair-handoff", workspace).returncode == 70
    assert run("getfacl", "-cp", target).stdout == before


def test_successful_process_recovery_with_large_actual_venv_under_60_seconds(
    host: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_m32_handoff_recovery import blocked_application
    from test_m32_one_milestone_happy_path import MILESTONE, PROJECT

    from syntra_build.application.implementation_recovery import (
        recover_successful_implementation_handoff,
    )
    from syntra_build.application.lifecycle import LifecycleCoordinator
    from syntra_build.infrastructure.persistence import open_database

    launcher, old_workspace, _cli, _repository = host
    fixture = launcher.parent / "recovery"
    fixture.mkdir(mode=0o755)
    database, data, git, job = blocked_application(fixture)
    gc.collect()
    workspace = data / "workspaces" / str(PROJECT) / str(MILESTONE)
    environment, count = populated_venv(workspace, extra_files=12000)
    assert count >= 13000
    for path in (fixture, data, *data.rglob("*"), database):
        if not path.is_symlink():
            shutil.chown(path, "syntra-build", "syntra-build")
            path.chmod(0o700 if path.is_dir() else 0o600)
    restrictive_worker_tree(environment)
    restrictive_worker_tree(workspace / "generated.py")
    launcher.write_text(
        launcher.read_text().replace(str(old_workspace.parents[2]), str(data))
    )
    acl = launcher.parent / "acl.py"
    acl.write_text(acl.read_text().replace(str(old_workspace.parents[2]), str(data)))
    calls = launcher.parent / "helper-subprocess-calls"
    acl.write_text(
        acl.read_text().replace(
            'if __name__ == "__main__":',
            "def record_subprocess(event, args):\n"
            "    if event == 'subprocess.Popen' and os.geteuid() == 0:\n"
            f"        with open({str(calls)!r}, 'a') as counter:\n"
            "            counter.write('1\\n')\n"
            "sys.addaudithook(record_subprocess)\n\n"
            'if __name__ == "__main__":',
        )
    )
    # The fixture driver is root, while the actual Git owner/checker is separate.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(workspace))
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "safe.directory")
    monkeypatch.setenv(
        "GIT_CONFIG_VALUE_1", str(data / "repositories" / str(PROJECT) / "repo.git")
    )
    interpreter = (environment / "bin/python").resolve()
    target_acl = run("getfacl", "-cp", interpreter).stdout
    assert as_user("syntra-build", "cat", workspace / "generated.py").returncode != 0
    original_run = subprocess.run
    timeouts: list[float] = []

    def observed_run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if command[:3] == ("bash", str(launcher), "--repair-handoff"):
            timeouts.append(kwargs["timeout"])
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", observed_run)
    with open_database(database) as db:
        before = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in ("codex_runs", "jobs", "job_attempts", "git_workspaces")
        }
        content = (workspace / "generated.py").read_bytes()
        start = time.monotonic()
        accepted = recover_successful_implementation_handoff(
            db,
            git,
            data,
            PROJECT,
            MILESTONE,
            job,
            repair_command=lambda path: (
                "bash",
                str(launcher),
                "--repair-handoff",
                str(path),
            ),
        )
        elapsed = time.monotonic() - start
        assert elapsed < 60
        print(f"actual venv entries={count} recovery_seconds={elapsed:.3f} timeout=60")
        assert timeouts == [60]
        assert db.execute("SELECT state FROM projects").fetchone()[0] == "PAUSED"
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "COMMITTING"
        for table, rows in before.items():
            assert [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] == rows
    with open_database(database) as restarted:
        assert (
            recover_successful_implementation_handoff(
                restarted,
                git,
                data,
                PROJECT,
                MILESTONE,
                job,
                repair_command=lambda path: (
                    "bash",
                    str(launcher),
                    "--repair-handoff",
                    str(path),
                ),
            )
            == accepted
        )
        for _ in range(100):
            assert LifecycleCoordinator(restarted).enqueue_due() == 0
    assert timeouts == [60, 60]
    # One privilege-drop verifier per handoff, independent of 14,000+ inodes.
    assert calls.read_text().splitlines() == ["1", "1"]
    assert as_user("syntra-build", "cat", workspace / "generated.py").returncode == 0
    status = as_user("syntra-build", "git", "-C", workspace, "status", "--porcelain")
    assert status.returncode == 0 and not status.stderr
    assert ".venv" not in status.stdout
    assert (workspace / "generated.py").read_bytes() == content
    assert run("getfacl", "-cp", interpreter).stdout == target_acl
