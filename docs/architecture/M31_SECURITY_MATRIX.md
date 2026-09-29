# M31 security hardening and acceptance matrix

M31 makes security decisions durable and deterministic. Security events are
append-only; a separate append-only resolution is trusted evidence that an active
condition no longer blocks an operation. Historical HIGH/CRITICAL events are never
deleted. SQLite remains authoritative and the `SecurityPolicy` is used by the
Gatekeeper at both evaluation and immediate pre-merge execution.

## Automated matrix

| Requirement / threat | Control | Automated evidence | Host-only acceptance | Status |
|---|---|---|---|---|
| SEC-001 separate identities | systemd user plus root-owned transition helper | `test_m30_operational_assets.py::test_systemd_unit_is_hardened`; `test_m31_security_assets.py::test_systemd_and_launcher_security_contract` | distinct UID and sudo checks below | HOST_ACCEPTANCE_REQUIRED |
| SEC-002 credential/database isolation | allowlisted environment and private control data | `test_m20_codex_runner.py::test_environment_allowlist_excludes_parent_secrets`; static host-policy test | real read-denial smoke below | HOST_ACCEPTANCE_REQUIRED |
| SEC-003 Architect advisory only | strict response schema and deterministic Gatekeeper | M24 contract/review tests; M26 eligibility tests | none | AUTOMATED |
| SEC-004 secret scan | exact-byte scanner, durable HIGH event, pre-provision design scan | `test_m21_change_validation.py::test_security_finding_is_atomic_and_clean_revalidation_resolves_only_scope`; `test_m18_provisioning_service.py::test_approved_design_secret_blocks_before_repository_mutation` | none | AUTOMATED |
| SEC-005 credential-free remotes | validated remote URLs and Git authentication boundary | M19 workspace and M21 identity tests | inspect deployed remotes | AUTOMATED |
| SEC-006 repository/worktree identity | M19/M21/M22/M26 exact identity checks | respective integration suites | none | AUTOMATED |
| SEC-007 prompt injection | no AI capabilities; strict schemas; trusted protected scope | `test_architect_adapter.py::test_hostile_architect_content_cannot_add_privileged_actions`; `test_m24_architect_review.py::test_live_pr_and_response_identity_mismatches_are_rejected`; `test_m20_codex_runner.py::test_hostile_codex_text_has_no_credentials_or_workflow_authority`; M21 protected-scope tests | filesystem denial remains in OS capability smoke | AUTOMATED capability boundary; HOST_ACCEPTANCE_REQUIRED for Unix denial |
| SEC-008 gate correlation | authorised responder and exact durable gate binding | M25 unauthorised/duplicate/stale tests | none | AUTOMATED |
| SEC-009 stale SHA | SHA-bound CI/review/human evidence plus pre-PUT recheck | M26 Gatekeeper stale-evidence and race tests | none | AUTOMATED |
| SEC-010 running install isolation | `/opt/syntra-build` root:syntra-build 0750 | static policy asset test | real read/write denial | HOST_ACCEPTANCE_REQUIRED |
| SEC-011 project isolation | serialized exact-workspace ACL grant/cleanup | M20 launcher contract tests | two-project smoke | HOST_ACCEPTANCE_REQUIRED |
| SEC-012 auditable blocking | migration 026, security policy, immutable history, real policy guards | `test_m31_security.py::test_migration_26_and_active_resolution_history`; `test_m18_provisioning_service.py::test_persisted_security_condition_blocks_all_provisioning_mutations`; `test_m22_pull_request_lifecycle.py::test_persisted_security_condition_blocks_push_and_pr_then_resolves`; `test_m26_gatekeeper.py::test_persisted_security_event_after_prepare_prevents_put` | none | AUTOMATED |
| SECURITY §§5–18 identity/credentials | early Telegram rejection, daemon-composed durable sink, service identities, empty worker environment | `test_m30_service.py::test_service_persists_unauthorised_telegram_event_without_routing`; `test_telegram.py::test_security_event_failure_does_not_route_unauthorised_update`; M20 tests | identity/ACL smoke | AUTOMATED transport/composition; HOST_ACCEPTANCE_REQUIRED for OS boundary |
| SECURITY §§19–38 source/workspace/Git/GitHub | exact identity, scan, protected paths, privileged guards | M18/M19/M21/M22/M26 integration suites | ACL inspection | AUTOMATED except Unix ACL behavior |
| SECURITY §§39–45 availability/data/logging | timeout/process-group kill, fixed reviewed launcher rlimits, WAL/backup, redaction | M20/M29/M30 suites; `test_m20_codex_runner.py::test_real_file_size_limit_fails_worker_without_oversized_file` exercises real `RLIMIT_FSIZE` without root | production helper values and host pressure smoke | PARTIAL — no Codex network namespace or hard memory limit |
| SECURITY §§46–54 recovery/testing | reconcile-before-replay and negative paths | M27 recovery suites; CI shell syntax step | recovery operator smoke | AUTOMATED except live provider/host behavior |

The matrix references existing tests where they already prove an invariant; it does
not claim that Unix users, ACLs, sudo or systemd can be proven in unprivileged CI.

## Filesystem and resource policy

`/opt/syntra-build` and `/etc/syntra-build` are root-owned, group-readable only by
`syntra-build` (0750); `config.json` is root:syntra-build 0640. Credential files
are syntra-build:syntra-build 0600 and the loader rejects symlinks, non-regular
files, foreign owners, and any group/other access. State, logs,
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

After review, install the exact approved application revision, then apply the host
assets in the order below. `install-service.sh` copies the reviewed M31 unit into
`/etc/systemd/system`; enforcement then idempotently confirms the final private root
modes without recursively altering worktrees, repositories, ACLs, or Git content.
Only after inspecting the effective unit and permissions should the service restart.
Run the production-path smoke against an isolated acceptance database and two
disposable registered M19 workspaces:

```bash
sudo scripts/host/setup-codex-worker.sh
sudo scripts/host/install-service.sh
sudo scripts/host/enforce-security-policy.sh
sudo systemctl daemon-reload
systemctl cat syntra-build.service
systemctl show syntra-build.service -p User -p Group -p UMask -p NoNewPrivileges
stat -c '%U:%G:%a %n' /var/lib/syntra-build \
  /var/lib/syntra-build/{backups,artifacts,workspaces,repositories} \
  /var/log/syntra-build
sudo systemctl restart syntra-build
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

Credential rotation does not change this model: rotate the service-owned 0600 secret,
restart the service, and verify the worker continues to observe only ABSENT/DENIED.
