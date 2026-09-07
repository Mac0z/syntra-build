# Syntra Build

Syntra Build is an AI-assisted software-delivery orchestration system. It
coordinates human decisions, architecture, implementation, CI, review, and
delivery while retaining authoritative workflow control.

This repository requires **Python 3.14** (`>=3.14,<3.15`). The production
control plane targets Ubuntu Linux on ARM64.

## Development setup

Create and activate a virtual environment, then install the package and its
development tools:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

The canonical local validation commands are:

```bash
# Unit tests
python -m pytest

# Lint and formatting validation
ruff check .
ruff format --check .

# Static type checking
python -m mypy
```

These checks require no network access or credentials after development
dependencies have been installed. Never commit credentials or local `.env`
files.

## Configuration

The application-facing configuration entry point is
`syntra_build.infrastructure.config.load_config`. It returns one immutable,
validated configuration object with groups for filesystem and database paths,
external integrations, scheduling, retries, logging, metrics, backups, and
resource thresholds. Production defaults use `/opt/syntra-build`,
`/etc/syntra-build`, `/var/lib/syntra-build`, and `/var/log/syntra-build`;
tests and development tools should inject temporary absolute roots instead.

Configuration may be supplied explicitly or through the documented
`SYNTRA_*` environment variables handled by the loader. Explicit values take
precedence. Credentials are accepted separately as `SecretInputs` (or from
the dedicated credential environment variables), are excluded from safe
configuration serialisation, and are redacted from representations. Supply
credentials through host-protected facilities in production. Never commit a
`.env` file or credentials.

## Project documentation

- [Implementation specification](SPEC.md)
- [Engineering instructions](AGENTS.md)
- [Design documents](docs/)
- [Architecture Decision Records](docs/adr/)
