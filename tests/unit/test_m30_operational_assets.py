from pathlib import Path

from syntra_build.admin import parser

ROOT = Path(__file__).parents[2]


def test_systemd_contract_and_scripts() -> None:
    unit = (ROOT / "deployment/systemd/syntra-build.service").read_text()
    assert "User=syntra-build" in unit
    assert "Group=syntra-build" in unit
    assert "ExecStart=/opt/syntra-build/venv/bin/python -m syntra_build.service" in unit
    assert "Restart=on-failure" in unit
    assert "TimeoutStopSec=45s" in unit
    assert "token" not in unit.casefold()
    assert "curl" not in unit and "|" not in unit
    install = (ROOT / "scripts/host/install-service.sh").read_text()
    assert "systemctl daemon-reload" in install
    assert "systemctl start" not in install
    assert "/etc/syntra-build/config.json" not in install
    upgrade = (ROOT / "scripts/host/upgrade.sh").read_text()
    assert upgrade.index("syntra_build.pre_upgrade") < upgrade.index("install-dev.sh")


def test_admin_parser_has_no_authority_escape_hatches() -> None:
    help_text = parser().format_help().casefold()
    for forbidden in (
        "set-state",
        "mark-ci",
        "approve-architect",
        "pass-human",
        "force-merge",
        "complete-milestone",
        " sql",
    ):
        assert forbidden not in help_text
