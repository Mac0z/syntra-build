from pathlib import Path

from syntra_build.application.change_validation import ProtectedPathPolicy
from syntra_build.infrastructure.change_validation import RegexSecretScanner

ROOT = Path(__file__).parents[2]


def test_deployment_security_contract() -> None:
    unit = (ROOT / "deployment/systemd/syntra-build.service").read_text()
    assert "User=syntra-build" in unit and "Group=syntra-build" in unit
    assert "PrivateTmp=true" in unit and "UMask=0077" in unit
    assert "NoNewPrivileges=true" not in unit
    helper = (ROOT / "scripts/host/syntra-codex-launch").read_text()
    assert "--no-new-privs" in helper
    assert "/usr/bin/prlimit --nproc=128 --nofile=1024 --fsize=104857600" in helper
    policy = (ROOT / "scripts/host/enforce-security-policy.sh").read_text()
    for group in ("sudo", "docker", "syntra-build"):
        assert group in policy


def test_expanded_protected_paths_are_deterministic() -> None:
    policy = ProtectedPathPolicy()
    for path in (
        ".github/workflows/ci.yml",
        ".github/actions/x/action.yml",
        "deployment/systemd/x",
        "scripts/host/setup.sh",
        "docs/SECURITY.md",
        "docs/security/model.md",
        "security/policy.py",
        "authentication/token.py",
    ):
        assert policy.protected(path)
        assert not policy.authorised(path, ("authorise .github/workflows/**",))


def test_secret_scanner_high_confidence_shapes_without_exposing_values() -> None:
    samples = (
        b"ghp_abcdefghijklmnopqrstuvwxyz123456",
        b"github_pat_abcdefghijklmnopqrstuvwxyz123456",
        b"-----BEGIN PRIVATE KEY-----",
        b"AKIAABCDEFGHIJKLMNOP",
        b"https://user:clearlyfakepassword@example.invalid/repo",
        b"password = 'clearly_fake_password_123'",
        b"123456789:abcdefghijklmnopqrstuvwxyzABCDEFGH",
        b"api_key=sk-proj-abcdefghijklmnopqrstuvwxyz",
    )
    scanner = RegexSecretScanner()
    for sample in samples:
        matches = scanner.scan(sample)
        assert matches
        assert all(sample.decode() not in match.fingerprint for match in matches)
