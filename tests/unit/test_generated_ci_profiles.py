import re
from hashlib import sha256

import pytest

from syntra_build.application.ci_profiles import (
    TRUSTED_CI_WORKFLOW_PATH,
    GeneratedCIProfile,
    GeneratedCIProfileError,
    select_ci_profile,
)


def approved_spec(version: str = "3.12") -> str:
    return (
        '# Example application\n\n```syntra-ci\nprofile = "python-pytest-v1"\n'
        f'python-version = "{version}"\n```\n'
    )


@pytest.mark.parametrize("version", ["3.12", "3.13", "3.14"])
def test_explicit_approved_profile_renders_repeatable_bytes(version: str) -> None:
    profile = select_ci_profile(approved_spec(version))

    assert profile.profile_id == "python-pytest-v1"
    assert profile.python_version == version
    assert profile.required_checks == ("tests",)
    assert profile.workflow_files() == {
        TRUSTED_CI_WORKFLOW_PATH: profile.workflow_content()
    }
    assert (
        profile.workflow_content()
        == select_ci_profile(approved_spec(version)).workflow_content()
    )
    assert f'python-version: "{version}"'.encode() in profile.workflow_content()


@pytest.mark.parametrize(
    "spec",
    [
        "# Python project using pytest on Python 3.12",
        '```toml\nprofile = "python-pytest-v1"\npython-version = "3.12"\n```',
        '```syntra-ci\nprofile = "node-test-v1"\npython-version = "3.12"\n```',
        '```syntra-ci\nprofile = "python-pytest-v2"\npython-version = "3.12"\n```',
        approved_spec("3.x"),
        approved_spec("3.15"),
        approved_spec("3.12.1"),
        approved_spec("${{ secrets.PYTHON_VERSION }}"),
        approved_spec('3.12"\npermissions = "write-all'),
        '```syntra-ci\nprofile = "python-pytest-v1"\n```',
        '```syntra-ci\npython-version = "3.12"\n```',
        '```syntra-ci\nprofile = "python-pytest-v1"\npython-version = 3.12\n```',
        '```syntra-ci\nprofile = []\npython-version = "3.12"\n```',
        approved_spec().replace('python-version = "3.12"', "python-version = true"),
        approved_spec().replace(
            'python-version = "3.12"',
            'python-version = "3.12"\nrun = "echo fake-green"',
        ),
        approved_spec().replace(
            'python-version = "3.12"',
            'python-version = "3.12"\npython-version = "3.13"',
        ),
        approved_spec() + approved_spec(),
        approved_spec() + approved_spec("3.14"),
        approved_spec().replace('python-version = "3.12"', "invalid TOML"),
        approved_spec().removesuffix("```\n"),
        approved_spec().replace("```syntra-ci", "``` syntra-ci"),
        approved_spec().replace("```syntra-ci", "```syntra-ci-extra"),
        approved_spec().replace("```syntra-ci", "~~~~syntra-ci"),
    ],
)
def test_unsupported_missing_or_invalid_selection_fails_closed(spec: str) -> None:
    with pytest.raises(GeneratedCIProfileError, match="Approve exactly one"):
        select_ci_profile(spec)


def test_profile_examples_within_other_fences_do_not_select_ci() -> None:
    with pytest.raises(GeneratedCIProfileError):
        select_ci_profile("````markdown\n" + approved_spec() + "````\n")

    actual = approved_spec("3.14")
    example = "~~~~markdown\n" + approved_spec("3.12") + "~~~~\n"
    assert select_ci_profile(example + actual).python_version == "3.14"


def test_malformed_selection_does_not_echo_untrusted_input() -> None:
    sentinel = "synthetic-confidential-value-do-not-output"
    spec = approved_spec().replace('python-version = "3.12"', sentinel)

    with pytest.raises(GeneratedCIProfileError) as raised:
        select_ci_profile(spec)

    assert sentinel not in str(raised.value)
    assert raised.value.__cause__ is None


def test_direct_profile_construction_cannot_bypass_allowlist() -> None:
    with pytest.raises(GeneratedCIProfileError):
        GeneratedCIProfile(profile_id="node-v1", python_version="3.12")
    with pytest.raises(GeneratedCIProfileError):
        GeneratedCIProfile(profile_id="python-pytest-v1", python_version="write-all")


def test_workflow_runs_exact_head_tests_without_privileged_credentials() -> None:
    workflow = select_ci_profile(approved_spec()).workflow_content().decode()

    assert "on:\n  pull_request:\n    branches: [main]\n" in workflow
    assert "permissions:\n  contents: read\n" in workflow
    assert "ref: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "name: tests" in workflow
    assert "PYTEST_ADDOPTS= python -I -m pytest -o addopts= --junitxml=" in workflow
    assert 'report.findall(".//testcase")' in workflow
    assert 'case.find("skipped") is None' in workflow
    assert "timeout-minutes: 15" in workflow
    assert "pytest>=8.4,<10" in workflow
    for forbidden in (
        "pull_request_target",
        "secrets.",
        "github.token",
        "id-token",
        "write-all",
        ": write",
        "continue-on-error",
        "|| true",
        "workflow_dispatch",
        "self-hosted",
        "cache:",
    ):
        assert forbidden not in workflow
    actions = re.findall(r"uses: ([^\s]+)", workflow)
    assert len(actions) == 2
    assert all(
        re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", action) for action in actions
    )


@pytest.mark.parametrize(
    ("version", "expected_hash"),
    [
        ("3.12", "02405ae2572e324b8c5424079242b3676a0fdbd2898d9639867733b10f993764"),
        ("3.13", "4154b55626cc5dc134aa9f2503c25e88f8503cff603315b8332445fa9410f91d"),
        ("3.14", "8959f31eed094250d371eef815bd668c4626fc30a1027e04aa3c324a899d6981"),
    ],
)
def test_v1_template_is_immutable(version: str, expected_hash: str) -> None:
    # New template bytes require a new profile version, preserving retries and
    # existing baseline evidence. Do not update these v1 hashes with a change.
    assert (
        sha256(select_ci_profile(approved_spec(version)).workflow_content()).hexdigest()
        == expected_hash
    )
