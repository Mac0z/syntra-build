"""Execute the generated workflow's shell steps with real pip and pytest offline.

The fixture uses a tiny PEP 517 backend requiring only the standard library, so
the test exercises an actual project build/install without downloading a build
backend. Installed pytest distributions supply a local, credential-free index.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from syntra_build.application.ci_profiles import GeneratedCIProfile


def _workflow_commands() -> list[str]:
    content = GeneratedCIProfile("python-pytest-v1", "3.14").workflow_content()
    lines = content.decode().splitlines()
    commands: list[str] = []
    for index, line in enumerate(lines):
        if line == "        run: |":
            block: list[str] = []
            for following in lines[index + 1 :]:
                if not following.startswith("          "):
                    break
                block.append(following.removeprefix("          "))
            commands.append("\n".join(block))
        elif line.startswith("        run: "):
            commands.append(line.removeprefix("        run: "))
    assert len(commands) == 2
    return commands


def _installed_wheelhouse(path: Path) -> None:
    """Repackage installed test tooling and its active runtime dependencies."""
    pending = ["pytest"]
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        distribution = metadata.distribution(name)
        normalized = distribution.metadata["Name"].replace("-", "_")
        wheel = path / f"{normalized}-{distribution.version}-py3-none-any.whl"
        assert distribution.files is not None
        with zipfile.ZipFile(wheel, "w") as archive:
            for file in distribution.files:
                # Installer-generated console scripts and bytecode are not
                # wheel inputs; their original dist-info entry points remain.
                if ".." in file.parts or "__pycache__" in file.parts:
                    continue
                source = Path(str(distribution.locate_file(file)))
                if source.is_file():
                    archive.write(source, str(file))
        for dependency in distribution.requires or []:
            requirement = Requirement(dependency)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)


def _helper_wheel(path: Path) -> None:
    name = "syntra_ci_fixture_helper"
    with zipfile.ZipFile(path / f"{name}-1-py3-none-any.whl", "w") as archive:
        archive.writestr(f"{name}.py", "VALUE = 42\n")
        archive.writestr(
            f"{name}-1.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: syntra-ci-fixture-helper\nVersion: 1\n",
        )
        archive.writestr(
            f"{name}-1.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(f"{name}-1.dist-info/RECORD", "")


@pytest.fixture(scope="module")
def wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("trusted-ci-wheels")
    _installed_wheelhouse(path)
    _helper_wheel(path)
    return path


def _python_project(path: Path, *, dev: bool) -> None:
    extra = (
        '\n[project.optional-dependencies]\ndev = ["syntra-ci-fixture-helper==1"]\n'
        if dev
        else ""
    )
    (path / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "offline_backend"\n'
        'backend-path = ["."]\n\n[project]\nname = "fx-fixture"\n'
        'version = "1.0"\nrequires-python = ">=3.12"\ndependencies = []\n' + extra,
        encoding="utf-8",
    )
    (path / "src").mkdir()
    (path / "src" / "fx_fixture.py").write_text(
        "from decimal import Decimal\n\n"
        "def reconcile(amounts: list[str]) -> Decimal:\n"
        '    return sum((Decimal(amount) for amount in amounts), Decimal("0"))\n',
        encoding="utf-8",
    )
    extra_metadata = (
        "Provides-Extra: dev\nRequires-Dist: syntra-ci-fixture-helper==1; "
        'extra == "dev"\n'
        if dev
        else ""
    )
    # PEP 517 build_wheel is invoked by real pip in its isolated build process.
    (path / "offline_backend.py").write_text(
        "from pathlib import Path\nimport zipfile\n\n"
        "def build_wheel(wheel_directory, config_settings=None, "
        "metadata_directory=None):\n"
        '    filename = "fx_fixture-1.0-py3-none-any.whl"\n'
        '    with zipfile.ZipFile(Path(wheel_directory) / filename, "w") as archive:\n'
        '        archive.write("src/fx_fixture.py", "fx_fixture.py")\n'
        '        archive.writestr("fx_fixture-1.0.dist-info/METADATA", '
        + repr(
            "Metadata-Version: 2.1\nName: fx-fixture\nVersion: 1.0\n" + extra_metadata
        )
        + ")\n"
        '        archive.writestr("fx_fixture-1.0.dist-info/WHEEL", '
        + repr(
            "Wheel-Version: 1.0\nGenerator: offline-fixture\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        )
        + ")\n"
        '        archive.writestr("fx_fixture-1.0.dist-info/RECORD", "")\n'
        "    return filename\n",
        encoding="utf-8",
    )


def _run_workflow(
    project: Path, wheelhouse: Path, *, pytest_addopts: str = ""
) -> list[subprocess.CompletedProcess[str]]:
    environment = {
        "PATH": os.defpath,
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheelhouse),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTEST_ADDOPTS": pytest_addopts,
    }
    venv = project.parent / f"{project.name}-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    environment["PATH"] = f"{venv / 'bin'}{os.pathsep}{os.defpath}"
    results: list[subprocess.CompletedProcess[str]] = []
    for command in _workflow_commands():
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        results.append(result)
        if result.returncode:
            break
    return results


@pytest.mark.parametrize("dev", [False, True])
def test_first_python_implementation_installs_and_runs_real_tests(
    tmp_path: Path, wheelhouse: Path, dev: bool
) -> None:
    project = tmp_path / "implementation"
    project.mkdir()
    _python_project(project, dev=dev)
    (project / "tests").mkdir()
    helper = "from syntra_ci_fixture_helper import VALUE\n" if dev else "VALUE = 42\n"
    (project / "tests" / "test_reconcile.py").write_text(
        "from decimal import Decimal\nfrom fx_fixture import reconcile\n"
        + helper
        + '\ndef test_reconcile():\n    assert reconcile(["10.25", "-3.10"]) '
        '== Decimal("7.15")\n' + "    assert VALUE == 42\n",
        encoding="utf-8",
    )

    results = _run_workflow(project, wheelhouse)

    assert len(results) == 2
    assert results[0].returncode == 0, results[0].stdout + results[0].stderr
    assert results[1].returncode == 0, results[1].stdout + results[1].stderr
    assert "1 passed" in results[1].stdout


@pytest.mark.parametrize(
    ("test_source", "expected_exit"),
    [
        ("", 5),
        ("def test_fail():\n    assert False\n", 1),
        (
            "import pytest\n@pytest.mark.skip(reason='no execution')\n"
            "def test_skipped():\n    assert True\n",
            1,
        ),
    ],
)
def test_workflow_fails_for_absent_or_failing_tests(
    tmp_path: Path, wheelhouse: Path, test_source: str, expected_exit: int
) -> None:
    project = tmp_path / "implementation"
    project.mkdir()
    _python_project(project, dev=False)
    if test_source:
        (project / "test_failure.py").write_text(test_source, encoding="utf-8")

    results = _run_workflow(project, wheelhouse)

    assert len(results) == 2
    assert results[0].returncode == 0, results[0].stdout + results[0].stderr
    assert results[1].returncode == expected_exit


def test_pytest_noop_options_and_local_module_shadows_cannot_fake_success(
    tmp_path: Path, wheelhouse: Path
) -> None:
    project = tmp_path / "implementation"
    project.mkdir()
    _python_project(project, dev=False)
    with (project / "pyproject.toml").open("a", encoding="utf-8") as file:
        file.write('\n[tool.pytest.ini_options]\naddopts = "--collect-only"\n')
    # Isolated Python invokes installed pytest and the trusted stdlib helpers,
    # rather than an accidental or hostile project module of the same name.
    for module in ("pytest", "pip", "subprocess", "xml"):
        (project / f"{module}.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (project / "test_failure.py").write_text(
        "def test_actually_runs():\n    assert False\n", encoding="utf-8"
    )

    results = _run_workflow(project, wheelhouse, pytest_addopts="--help")

    assert len(results) == 2
    assert results[0].returncode == 0, results[0].stdout + results[0].stderr
    assert results[1].returncode == 1
    assert "1 failed" in results[1].stdout


@pytest.mark.parametrize("setup", ["absent", "invalid", "dependency-error"])
def test_workflow_setup_errors_fail_instead_of_running_fake_green_tests(
    tmp_path: Path, wheelhouse: Path, setup: str
) -> None:
    project = tmp_path / "implementation"
    project.mkdir()
    if setup == "invalid":
        (project / "pyproject.toml").write_text("broken TOML", encoding="utf-8")
    elif setup == "dependency-error":
        _python_project(project, dev=True)
        backend = project / "offline_backend.py"
        backend.write_text(
            backend.read_text().replace(
                "syntra-ci-fixture-helper==1", "unavailable-synthetic-ci-dependency==1"
            ),
            encoding="utf-8",
        )
    (project / "test_green.py").write_text("def test_green():\n    assert True\n")

    results = _run_workflow(project, wheelhouse)

    assert len(results) == 1
    assert results[0].returncode != 0
    assert "1 passed" not in results[0].stdout
