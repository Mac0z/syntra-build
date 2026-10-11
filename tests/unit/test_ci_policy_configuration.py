"""Offline validation of the bounded missing-CI policy and central settings."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from syntra_build.application.ci_polling import MissingCIPolicy
from syntra_build.infrastructure.config import (
    CIConfig,
    ConfigurationError,
    load_config,
    load_host_config,
)

NOW = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)


def test_missing_ci_defaults_are_bounded_and_use_the_injected_clock() -> None:
    config = load_config(environ={})
    policy = MissingCIPolicy(
        config.ci.missing_grace_seconds, config.ci.missing_max_observations
    )

    assert config.ci == CIConfig(600.0, 20)
    assert policy.deadline_at(NOW) == NOW + timedelta(minutes=10)
    assert policy.deadline_at(NOW + timedelta(days=1)) == NOW + timedelta(
        days=1, minutes=10
    )


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        True,
        False,
        float("nan"),
        float("inf"),
        86_400.1,
        1e308,
        10**1000,
    ],
)
def test_missing_ci_grace_rejects_unsafe_numeric_values(value: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        MissingCIPolicy(grace_seconds=value)
    with pytest.raises(ConfigurationError, match="ci.missing_grace_seconds"):
        CIConfig(missing_grace_seconds=value)


def test_missing_ci_grace_accepts_the_documented_one_day_maximum() -> None:
    config = load_config(
        {"ci": {"missing_grace_seconds": 86_400}},
        environ={},
    )
    policy = MissingCIPolicy(grace_seconds=config.ci.missing_grace_seconds)

    assert config.ci.missing_grace_seconds == 86_400
    assert policy.deadline_at(NOW) == NOW + timedelta(days=1)


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "2", None])
def test_missing_ci_observation_cap_requires_positive_integer(value: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        MissingCIPolicy(max_observations=cast(int, value))
    with pytest.raises(ConfigurationError, match="ci.missing_max_observations"):
        CIConfig(missing_max_observations=cast(int, value))


def test_missing_ci_environment_is_loaded_centrally_and_explicit_values_win() -> None:
    environment = {
        "SYNTRA_CI_MISSING_GRACE_SECONDS": "45.5",
        "SYNTRA_CI_MISSING_MAX_OBSERVATIONS": "4",
    }
    from_environment = load_config(environ=environment)
    explicit = load_config(
        {"ci": {"missing_grace_seconds": 12.5, "missing_max_observations": 2}},
        environ=environment,
    )

    assert from_environment.ci == CIConfig(45.5, 4)
    assert explicit.ci == CIConfig(12.5, 2)


@pytest.mark.parametrize(
    ("key", "value", "field"),
    [
        ("SYNTRA_CI_MISSING_GRACE_SECONDS", "0", "missing_grace_seconds"),
        ("SYNTRA_CI_MISSING_GRACE_SECONDS", "nan", "missing_grace_seconds"),
        ("SYNTRA_CI_MISSING_GRACE_SECONDS", "inf", "missing_grace_seconds"),
        ("SYNTRA_CI_MISSING_GRACE_SECONDS", "86400.1", "missing_grace_seconds"),
        ("SYNTRA_CI_MISSING_GRACE_SECONDS", "1e308", "missing_grace_seconds"),
        ("SYNTRA_CI_MISSING_GRACE_SECONDS", "true", "missing_grace_seconds"),
        ("SYNTRA_CI_MISSING_MAX_OBSERVATIONS", "0", "missing_max_observations"),
        ("SYNTRA_CI_MISSING_MAX_OBSERVATIONS", "1.5", "missing_max_observations"),
        ("SYNTRA_CI_MISSING_MAX_OBSERVATIONS", "true", "missing_max_observations"),
    ],
)
def test_invalid_missing_ci_environment_fails_before_runtime(
    key: str, value: str, field: str
) -> None:
    with pytest.raises(ConfigurationError, match=field):
        load_config(environ={key: value})


def test_explicit_missing_ci_configuration_rejects_overflowing_deadlines() -> None:
    with pytest.raises(ConfigurationError, match="ci.missing_grace_seconds"):
        load_config({"ci": {"missing_grace_seconds": 1e308}}, environ={})


def test_missing_ci_settings_survive_safe_manifest_and_host_loading(
    tmp_path: Path,
) -> None:
    config = load_config(
        {"ci": {"missing_grace_seconds": 75.5, "missing_max_observations": 3}},
        environ={},
    )
    manifest = config.safe_dict()
    host_config = tmp_path / "config.json"
    host_config.write_text(json.dumps(manifest, default=str), encoding="utf-8")

    restored = load_host_config(
        host_config,
        telegram_token_path=tmp_path / "absent-telegram-token",
        github_token_path=tmp_path / "absent-github-token",
        architect_api_key_path=tmp_path / "absent-architect-key",
    )

    assert manifest["ci"] == {
        "missing_grace_seconds": 75.5,
        "missing_max_observations": 3,
    }
    assert "secrets" not in manifest
    assert restored.ci == config.ci
