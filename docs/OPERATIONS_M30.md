# M30 operations

The `syntra-build-service` process composes schema bootstrap, M27 recovery,
M29 health/metrics, the existing durable scheduler, and a cooperative daily
backup check. It does **not** add M32 autonomous project orchestration or new
worker executors.

## Administration and backups

All commands accept `--config /etc/syntra-build/config.json` before the
subcommand. Use `version`, `health`, `projects [--active|--waiting]`,
`status PROJECT`, `integrity`, `backup [--reason manual]`,
`restore-verify FILE`, and `reconcile [--apply]`. Health exits zero when ready
(including usable degraded health) and nonzero otherwise. Reconcile defaults
to observation only; apply fails closed when trusted provider composition is
not available and cannot set workflow evidence or state directly.

Backups use SQLite's online backup API, are verified before atomic publication,
and are named with UTC time, schema, and a bounded reason. Automatic scheduling
checks hourly and creates at most the backup needed for a rolling 24-hour
period. Retention deletes recognised, non-symlink backups strictly older than
`retention_days`; the boundary and new backup are retained. Setting
`backups.enabled=false` disables only automatic backups. Manual,
pre-migration, and pre-upgrade safety backups remain available. `restore-verify`
copies an operator-selected regular non-symlink file into a private temporary
directory and performs full integrity, foreign-key, and exact migration-history
validation; it never replaces production.

Existing databases are backed up and verified before the first outstanding
migration. New and current schema databases do not receive unnecessary safety
backups. Schema remains 25.

## Install, upgrade, and systemd

Bootstrap the host as documented in `DEVELOPMENT_DEPLOYMENT.md`. For the first
upgrade from a pre-M30 release, stop the service and use Python's SQLite online
backup API from the old environment, validate `PRAGMA integrity_check` and
`PRAGMA foreign_key_check`, and retain that file before installing M30. Later
upgrades use `scripts/host/upgrade.sh EXACT_40_CHARACTER_SHA`; it invokes the
currently installed admin CLI for a verified `pre-upgrade` backup **before**
`install-dev.sh` replaces the application. Failure aborts installation.

As root, install (but do not start) the reviewed unit with
`scripts/host/install-service.sh`; add `--enable` only when wanted. Then:

```console
sudo systemctl start syntra-build
sudo systemctl is-active syntra-build
curl http://127.0.0.1:9464/health
curl http://127.0.0.1:9464/ready
/opt/syntra-build/venv/bin/syntra-build-admin status PROJECT
sudo journalctl -u syntra-build
sudo systemctl stop syntra-build
/opt/syntra-build/venv/bin/syntra-build-admin integrity
sudo systemctl start syntra-build
```

Startup binds health in STARTING/RECOVERING, keeps the scheduler drained, runs
M27 reconciliation, and only then releases dispatch. SIGTERM/SIGINT immediately
sets DRAINING, stops new claims and backup checks, gives threads a bounded unit
stop window, closes HTTP/SQLite, and leaves durable uncertain work for M27.

For rollback, stop the service, preserve the current database, identify and
`restore-verify` the pre-upgrade backup, and install the prior exact revision.
Confirm its supported schema before any operator-managed database replacement:
an older binary must never blindly open a newer schema. Production restore is
intentionally manual and is not an M30 CLI command. M31 hardening and M32 full
autonomous workflow remain deferred.
