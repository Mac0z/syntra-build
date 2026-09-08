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

## Logging

`syntra_build.infrastructure.logging.configure_logging` configures the
`syntra_build` logger hierarchy from the validated `ApplicationConfig`. The
production default emits one JSON object per line to standard error for
systemd/journald collection; `logging.level` selects the standard minimum
severity and `logging.structured = false` enables human-readable development
output.

Operations can establish task-local correlation, project, milestone, job, and
gate identifiers with `logging_context`. Context is inherited by nested code,
isolated between asynchronous tasks, and restored when its scope exits. Log
calls should provide a stable `event` through `extra` and place structured
details in `metadata`. Sensitive keys and M1 secret wrappers are redacted, but
callers must still never put credentials, authorization values, private keys,
environment dumps, or unrestricted provider payloads in logs.

## Persistence

SQLite is Syntra Build's persistent state store. Database bootstrap uses the
validated `database.sqlite_path` configuration, enables foreign keys and WAL
mode, applies source-controlled migrations automatically, and performs a
lightweight integrity check. The returned connection is caller-owned and must
be closed when no longer needed.

Development and tests should configure safe local or temporary database paths;
tests must never use the production database. Schema changes belong in explicit
migrations and should not normally be made by editing a database manually.

Schema version 2 adds the M7 `projects` and append-only `state_transitions`
tables. Schema version 3 adds M8 milestones and normalized dependencies, and
extends transition history for both project and milestone records. Upgrades
preserve existing M7 history. Downgrading an
already-used database is not supported; restore a pre-migration backup rather
than deleting transition history or manually changing the schema.

Schema version 4 adds M9 jobs, append-only job attempts, typed worker classes,
retry timing, and shared `JOB` transition history. Terminal attempts cannot be
updated and no attempt may be deleted. Because SQLite migrations are forward
only, downgrade requires restoring a version-3 backup.

Project lifecycle changes are accepted only through the explicit domain
transition policy and the SQLite project repository. The repository uses an
expected-current-state check and commits the current state and its history row
in one transaction. `activity` remains independently updateable status text;
it is never interpreted as lifecycle state.

## Telegram gateway

Telegram is the initial messaging transport. Enable it through the central
configuration and provide an allowlist of stable, numeric Telegram user IDs;
usernames are not authorization identities. Supply the bot token separately as
a protected secret input, never as ordinary project configuration.

The M5 adapter performs bounded long polls and plain-text sends only. It
normalizes authorized text updates and exposes provider update/message IDs so a
later durable consumer can manage offsets and duplicate delivery. It neither
stores offsets nor owns command routing or durable duplicate processing.

## Commands

The provider-neutral application router supports `ping`, `health`, `projects`,
`status <project>`, `pause <project>`, `resume <project>`, and
`cancel <project>`. An optional leading slash is accepted. Project reads and
state-change requests use injected application service boundaries; routing does
not itself apply project lifecycle transitions.

## Project documentation

- [M6A target-host development deployment](docs/DEVELOPMENT_DEPLOYMENT.md)
- [Implementation specification](SPEC.md)
- [Engineering instructions](AGENTS.md)
- [Design documents](docs/)
- [Architecture Decision Records](docs/adr/)
