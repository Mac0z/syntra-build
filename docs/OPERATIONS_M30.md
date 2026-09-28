# M30 operations

The continuously running `syntra-build-service` process composes schema bootstrap, M27 recovery,
M29 health/metrics, the existing durable scheduler, and a cooperative daily
backup check. When Telegram is enabled it continuously runs the existing bounded
poll/router with its durable cursor, so health, status, active-project, and
waiting-for-me controls remain available. It does **not** add M32 autonomous
project orchestration or new worker executors.

## Administration and backups

All commands accept `--config /etc/syntra-build/config.json` before the
subcommand. Use `version`, `health`, `projects [--active|--waiting]`,
`status PROJECT`, `integrity`, `backup [--reason manual]`,
`restore-verify FILE`, and `reconcile [--apply]`. Health exits zero when ready
(including usable degraded health) and nonzero otherwise. Reconcile defaults
to observation only; apply fails closed when trusted provider composition is
not available and cannot set workflow evidence or state directly. Read commands
open the database without bootstrapping or migrating it and fail closed unless
its migration history exactly matches the running code. Service startup is the
documented migration path; manual backup can preserve an older supported schema.

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
upgrade from a pre-M30 release, stop the service and run
`scripts/host/upgrade.sh EXACT_40_CHARACTER_SHA` from the exact reviewed M30
checkout. The wrapper runs `python3.14` with that checkout's `src` on
`PYTHONPATH`, creates and verifies a `PRE_UPGRADE` backup, and only then invokes
`install-dev.sh`. This works before the M30 CLI is installed; backup failure
aborts before package replacement or the `REVISION` update. The same reviewed
path is used for later upgrades.

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

Startup loads the real protected host configuration, binds health in
STARTING/RECOVERING, keeps the scheduler drained, and runs M27 reconciliation.
GitHub-enabled hosts compose the released PR, CI, Gatekeeper, and workspace
observers; human-gate restoration is always composed. Provider paths that are
not configured remain unavailable and therefore fail closed. Only after recovery
does dispatch begin. SIGTERM/SIGINT immediately
sets DRAINING, stops new claims and backup checks, gives threads a bounded unit
stop window, closes HTTP/SQLite, and leaves durable uncertain work for M27.

For rollback, stop the service, preserve the current database, identify and
`restore-verify` the pre-upgrade backup, and install the prior exact revision.
Confirm its supported schema before any operator-managed database replacement:
an older binary must never blindly open a newer schema. Production restore is
intentionally manual and is not an M30 CLI command. M31 hardening and M32 full
autonomous workflow remain deferred.
