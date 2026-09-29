# M31 security hardening and acceptance matrix

M31 makes security decisions durable and deterministic. Security events are
append-only; a separate append-only resolution is trusted evidence that an active
condition no longer blocks an operation. Historical HIGH/CRITICAL events are never
deleted. SQLite remains authoritative and the `SecurityPolicy` is used by the
Gatekeeper at both evaluation and immediate pre-merge execution.

## Automated matrix

| Requirement / threat | Control | Automated evidence | Host-only acceptance | Status |
|---|---|---|---|---|
| SEC-001 separate identities | systemd service user and root-owned identity-transition helper | `test_m30_operational_assets.py`, `test_m31_security_assets.py` | M31 smoke below | automated + host |
| SEC-002 credential/database isolation | rebuilt allowlist environment; 0700 data root; 0600 DB | M20 runner tests; `test_m31_security_assets.py` | M31 smoke | automated + host |
| SEC-003 Architect advisory only | strict response contracts; no GitHub capability | M24 Architect review tests | none | automated |
| SEC-004 secret scan | exact-byte offline scanner and durable HIGH condition | M21 validation tests; `test_m31_security_assets.py` | none | automated |
| SEC-005 credential-free remotes | repository identity validation | M19/M21 workspace tests | none | automated |
| SEC-006 repository/worktree identity | M19/M21/M22/M26 deterministic checks | their integration suites | none | automated |
| SEC-007 prompt injection | capability separation, strict schemas, protected scope supplied only by trusted caller | Architect contract and protected-path tests | M31 smoke | automated + host |
| SEC-008 gate correlation | authorised responder and exact durable gate binding | M25 human-intervention tests | none | automated |
| SEC-009 stale SHA | current-head CI, Architect and human evidence guards; pre-PUT recheck | M26 Gatekeeper tests | none | automated |
| SEC-010 running install isolation | `/opt/syntra-build` root:syntra-build 0750 | static policy test | M31 smoke | host required |
| SEC-011 project isolation | serialized per-workspace ACL grant and cleanup | M20 helper tests | two-project M31 smoke | host required |
| SEC-012 auditable blocking | migration 026, bounded enums, append-only events/resolutions, Gatekeeper policy | `test_m31_security.py`, M26 tests | none | automated |
| SECURITY §§5–18 identity/credentials | identities, minimal environment, deterministic adapters | M18–M24 suites | M31 smoke | covered |
| SECURITY §§19–38 source/workspace/Git/GitHub | exact identities, scan, protected paths, Gatekeeper | M19/M21/M22/M26 suites | ACL inspection | covered |
| SECURITY §§39–45 availability/data/logging | timeouts, process group kill, rlimits, WAL/backup, redaction | M20/M29/M30 suites and static M31 test | resource smoke | covered |
| SECURITY §§46–54 recovery/testing | reconcile-before-replay and negative paths | M27 recovery and this matrix | recovery operator smoke | covered |

The matrix references existing tests where they already prove an invariant; it does
not claim that Unix users, ACLs, sudo or systemd can be proven in unprivileged CI.

## Filesystem and resource policy

`/opt/syntra-build` and `/etc/syntra-build` are root-owned, group-readable only by
`syntra-build` (0750); configuration and secret files are 0640. State, logs,
artifacts and backups are `syntra-build:syntra-build` 0700, while SQLite DB/WAL/SHM
are 0600. The service uses `UMask=0077`. The worker home is 0700. The reviewed
enforcement script normalizes existing control data without recursively changing
workspace ACLs or Git content.

The trusted service deliberately does **not** set systemd `NoNewPrivileges`: it owns
one exact sudo rule for the root argument-validating launcher. The launcher drops to
`syntra-codex` with `setpriv --no-new-privs`, an empty allowlisted environment, a
global ACL lock, and fixed `nproc=128`, `nofile=1024`, and 100 MiB file-size limits.
Memory is not hard-limited because a brittle address-space ceiling could prevent the
Codex runtime from starting; timeout, process, file, disk, scheduler and concurrency
limits provide the initial bounded model.

## Raspberry Pi host acceptance

After review, install the assets and run the production-path smoke against an isolated
acceptance database and two disposable registered M19 workspaces:

```bash
sudo scripts/host/setup-codex-worker.sh
sudo scripts/host/enforce-security-policy.sh
sudo systemctl daemon-reload && sudo systemctl restart syntra-build
sudo -u syntra-build python -m syntra_build.m20_smoke --help
namei -l /opt/syntra-build /etc/syntra-build /var/lib/syntra-build/syntra.db
stat -c '%U:%G:%a %n' /opt/syntra-build /etc/syntra-build \
  /etc/syntra-build/config.json /var/lib/syntra-build /var/lib/syntra-build/syntra.db \
  /var/log/syntra-build /usr/local/libexec/syntra-codex-launch \
  /etc/sudoers.d/syntra-codex-launch
id syntra-build; id syntra-codex; getfacl /var/lib/syntra-build/workspaces
sudo -u syntra-codex test ! -r /etc/syntra-build/config.json
sudo -u syntra-codex test ! -r /var/lib/syntra-build/syntra.db
sudo -u syntra-codex test ! -r /opt/syntra-build/pyproject.toml
sudo -u syntra-codex test ! -r /var/log/syntra-build
sudo -u syntra-codex sudo -n true && exit 1 || true
sudo -u syntra-build sudo -n /bin/true && exit 1 || true
sudo visudo -cf /etc/sudoers.d/syntra-codex-launch
```

Install a disposable fake executable at an approved Codex path and invoke
`m20_smoke` first for Project A and then Project B. It must print only PASS/FAIL for
effective UID, assigned read/write, cross-project workspace/repository denial,
control paths, Docker socket and sudo denial, and credential-name absence. After each
run, `getfacl` must show no `syntra-codex` entry on the completed workspace. Running
the command via `systemd-run --uid=syntra-build --wait` additionally proves the
service identity can traverse the narrow helper after removal of control-plane
`NoNewPrivileges`. Never print protected file contents or environment values.

Credential rotation does not change this model: rotate the root-owned 0640 secret,
restart the service, and verify the worker continues to observe only ABSENT/DENIED.
