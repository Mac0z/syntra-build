"""Versioned, deterministic CI policy selected only from approved SPEC metadata.

Profile versions freeze the workflow bytes. Changing a template, action pin or
dependency/test policy requires a new profile ID; persisted v1 baselines must
continue to reproduce the exact original bytes.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

TRUSTED_CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
PYTHON_PYTEST_PROFILE = "python-pytest-v1"
SUPPORTED_PYTHON_VERSIONS = frozenset({"3.12", "3.13", "3.14"})

# Verified release commits, rather than mutable action tags:
# https://github.com/actions/checkout/commit/11bd71901bbe5b1630ceea73d27597364c9af683
# https://github.com/actions/setup-python/commit/e797f83bcb11b83ae66e0230d6156d7c80228e7c
_CHECKOUT_V4_2_2 = "11bd71901bbe5b1630ceea73d27597364c9af683"
_SETUP_PYTHON_V6_0_0 = "e797f83bcb11b83ae66e0230d6156d7c80228e7c"

_PROFILE_INSTRUCTION = (
    "Approve exactly one SPEC.md ```syntra-ci block with profile = "
    '"python-pytest-v1" and python-version = "3.12", "3.13" or "3.14". '
    "Other stacks require a separately supported, approved CI profile."
)

# PEP 621 projects may declare a dev extra for their test dependencies. This
# fixed policy never accepts arbitrary commands, paths or pip flags from SPEC.
_INSTALL_STEPS = """python -I - <<'PY'
from pathlib import Path
import subprocess
import sys
import tomllib

path = Path("pyproject.toml")
if not path.is_file():
    raise SystemExit("python-pytest-v1 requires a pyproject.toml Python project")
metadata = tomllib.loads(path.read_text(encoding="utf-8"))
project = metadata.get("project")
if not isinstance(project, dict):
    raise SystemExit("python-pytest-v1 requires PEP 621 [project] metadata")
extras = project.get("optional-dependencies", {})
if not isinstance(extras, dict):
    raise SystemExit("project.optional-dependencies must be a table")
target = ".[dev]" if "dev" in extras else "."
subprocess.run(
    [sys.executable, "-I", "-m", "pip", "install", target, "pytest>=8.4,<10"],
    check=True,
)
PY"""

_TEST_STEPS = """report_dir="$(mktemp -d)"
trap 'rm -rf "$report_dir"' EXIT
PYTEST_ADDOPTS= python -I -m pytest -o addopts= --junitxml="$report_dir/results.xml"
python -I - "$report_dir/results.xml" <<'PY'
import sys
from xml.etree import ElementTree

report = ElementTree.parse(sys.argv[1])
cases = report.findall(".//testcase")
if not any(case.find("skipped") is None for case in cases):
    raise SystemExit(
        "python-pytest-v1 requires at least one executed, non-skipped test"
    )
if any(
    case.find("failure") is not None or case.find("error") is not None
    for case in cases
):
    raise SystemExit("python-pytest-v1 test report contains failures or errors")
PY"""


class GeneratedCIProfileError(ValueError):
    """Approved design does not select a supported, valid CI policy."""


@dataclass(frozen=True, slots=True)
class GeneratedCIProfile:
    profile_id: str
    python_version: str

    def __post_init__(self) -> None:
        if (
            self.profile_id != PYTHON_PYTEST_PROFILE
            or self.python_version not in SUPPORTED_PYTHON_VERSIONS
        ):
            raise GeneratedCIProfileError(_PROFILE_INSTRUCTION)

    @property
    def required_checks(self) -> tuple[str, ...]:
        return ("tests",)

    def workflow_content(self) -> bytes:
        install = "\n".join(f"          {line}" for line in _INSTALL_STEPS.splitlines())
        tests = "\n".join(f"          {line}" for line in _TEST_STEPS.splitlines())
        # No code or expressions from approved prose enter this template. The
        # only substituted design value is one allowlisted Python minor version.
        content = f"""# Trusted Syntra CI profile: python-pytest-v1
# Workflow policy is owned by trusted provisioning, not implementation workers.
name: CI

on:
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  tests:
    name: tests
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - name: Check out the exact PR head
        uses: actions/checkout@{_CHECKOUT_V4_2_2} # v4.2.2
        with:
          ref: ${{{{ github.event.pull_request.head.sha }}}}
          persist-credentials: false

      - name: Set up approved Python
        uses: actions/setup-python@{_SETUP_PYTHON_V6_0_0} # v6.0.0
        with:
          python-version: "{self.python_version}"

      - name: Install project and test dependencies
        shell: bash
        run: |
{install}

      - name: Run real repository tests
        shell: bash
        run: |
{tests}
"""
        return content.encode("utf-8")

    def workflow_files(self) -> dict[str, bytes]:
        return {TRUSTED_CI_WORKFLOW_PATH: self.workflow_content()}


def select_ci_profile(approved_spec: str) -> GeneratedCIProfile:
    """Fail closed unless one explicit approved metadata block is valid.

    Stack names and runtime mentions elsewhere in prose are not selectors.
    Ordinary Markdown fences are tracked so an example inside a code block
    cannot authorize a profile. Only an exact triple-backtick syntra-ci fence
    is accepted, and the TOML table has exactly the two documented keys.
    """
    blocks = _profile_blocks(approved_spec)
    if len(blocks) != 1:
        raise GeneratedCIProfileError(_PROFILE_INSTRUCTION)
    try:
        metadata = tomllib.loads(blocks[0])
    except tomllib.TOMLDecodeError:
        # Do not echo arbitrary approved input into an operator error/log.
        raise GeneratedCIProfileError(
            f"CI profile metadata must be valid TOML. {_PROFILE_INSTRUCTION}"
        ) from None
    if set(metadata) != {"profile", "python-version"}:
        raise GeneratedCIProfileError(
            "CI profile metadata has missing or unsupported keys. "
            + _PROFILE_INSTRUCTION
        )
    profile, version = metadata["profile"], metadata["python-version"]
    if not isinstance(profile, str) or not isinstance(version, str):
        raise GeneratedCIProfileError(_PROFILE_INSTRUCTION)
    return GeneratedCIProfile(profile_id=profile, python_version=version)


def _profile_blocks(spec: str) -> list[str]:
    blocks: list[str] = []
    fence: str | None = None
    profile_block = False
    lines: list[str] = []
    for line in spec.splitlines():
        if fence is not None:
            if re.fullmatch(
                re.escape(fence[0]) + "{" + str(len(fence)) + ",}[ \\t]*", line
            ):
                if profile_block:
                    blocks.append("\n".join(lines))
                fence, profile_block, lines = None, False, []
            elif profile_block:
                lines.append(line)
            continue
        match = re.fullmatch(r"(`{3,}|~{3,})(.*)", line)
        if match is None:
            continue
        fence, info = match.groups()
        profile_block = info.rstrip() == "syntra-ci"
        if info.strip().startswith("syntra-ci") and (
            not profile_block or fence != "```"
        ):
            raise GeneratedCIProfileError(_PROFILE_INSTRUCTION)
    if profile_block:
        raise GeneratedCIProfileError(
            f"CI profile metadata fence is unterminated. {_PROFILE_INSTRUCTION}"
        )
    return blocks
