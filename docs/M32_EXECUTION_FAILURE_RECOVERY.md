# M32 implementation execution and handoff recovery

## Successful process with unreadable generated files

Confirmed incident: project `b1fde722-ecd7-46a6-91c2-f0f6e69d193a`, milestone
`c7c688c3-4844-4def-982f-017483ff5b12`, failed job
`afb29226-032d-4d1d-b884-953cfed5bbb0`. The durable Codex process succeeded with
exit zero, but worker-owned `0700` directories and `0600` files were inaccessible
to `syntra-build`. The scheduler exception blocked implementation completion.
This is a filesystem handoff failure with existing code, not empty output.

The launcher now repairs existing source ACLs and their masks explicitly, checks
effective access as `syntra-build`, and then removes temporary worker grants.
Repair runs on success, nonzero exit, signals, explicit cleanup, and stale-grant
reconciliation before another launch. The global isolation lock covers every
operation. A failed repair or revocation exits 70 and prevents a new launch.
SIGKILL cannot run a trap; explicit cleanup or the next launch reconciles it.
Root operations use `O_NOFOLLOW` directory/file descriptors and reject escaping
symlinks, hard links and unexpected filesystems. Source content and ownership are
preserved. `.git` is excluded from source repair; protected repository metadata
gets only temporary worker read access. Worker access to the control plane is
not expanded. The Code Mode profile and runtime attestation remain required.

A service-side cleanup failure raises `CodexHandoffError` after recording the
original process result. It does not replace a durable success with failure or
ignore an incomplete handoff. Scheduler exception events include `error_type`,
`safe_category`, job/project/milestone/correlation identifiers and the fixed
error ID. They omit exception messages, tracebacks, prompts and file contents.

### Operator deployment and recovery procedure

Perform these steps only after this PR has been reviewed and separately approved
for deployment. No step below has been executed against the live project.

1. Leave the incident project `BLOCKED`. Drain and stop the service using the
   existing maintenance procedure (`sudo systemctl stop syntra-build`); confirm
   no worker remains. Reconcile stale active jobs with the supported M27 route
   before recovery. Do not edit SQLite, delete history, reset counters, or use
   `recover-empty-implementation` for this incident.
2. As the service identity, run `syntra-build-admin backup --reason pre-upgrade`,
   note its returned path, then `syntra-build-admin restore-verify <backup-path>`.
   Preserve a root-readable copy of the worktree and protected run artifacts.
   Record the exact source revision, failed job, run/worktree IDs and Git HEAD /
   branch from the operator's existing protected incident evidence.
3. Deploy the reviewed exact source SHA through
   `scripts/host/upgrade.sh <reviewed-40-character-sha>` as the service user from
   that checkout. Deploy the host helpers separately, as root from the same
   checkout: `scripts/host/setup-codex-worker.sh`. This installs both
   `syntra-codex-launch` and `syntra-codex-acl.py`, root owned and mode 0755.
   Require Ubuntu's `acl` and `python3` packages. Do not run a paid Codex smoke
   invocation against the incident. Existing runtime/tool-host compatibility
   attestation and worker isolation requirements still apply.
4. With the service stopped, explicitly run as `syntra-build` (using the deployed
   venv's CLI and existing config):

   ```sh
   syntra-build-admin recover-successful-implementation-handoff \
     b1fde722-ecd7-46a6-91c2-f0f6e69d193a \
     c7c688c3-4844-4def-982f-017483ff5b12 \
     afb29226-032d-4d1d-b884-953cfed5bbb0
   ```

   The command requires the failed scheduler job/attempt, exactly matching
   successful run, approved IMPLEMENT task and documents, exact managed
   worktree, and the correlated persisted blockers. Supported milestone resume
   states are `CODING` and `VALIDATING_CHANGES`; the project must have blocked
   from `BUILDING`. Active jobs and unresolved security blockers are rejected.
   The root launcher performs `--repair-handoff` under its isolation lock.
   Trusted Git registration, common repository, remote, HEAD and branch and
   non-empty effective changes are verified after repair. Normal M21 validation
   must accept the current diff. Findings are retained on rejection; no correction
   invocation is scheduled by this command.
5. Confirm the command reports
   `change_set=<id> project_remains_paused=true provider_invoked=false`. Inspect
   `syntra-build-admin status b1fde722-ecd7-46a6-91c2-f0f6e69d193a`: project must
   be `PAUSED` with resume state `BUILDING`, milestone `COMMITTING`. The transition
   history binds the failed job, successful run, worktree, accepted change set and
   exact diff. Process evidence, failed attempts and retry/cycle counts remain
   unchanged. No job, Codex invocation, implementation regeneration, commit,
   push, PR or merge is performed by recovery.
6. Repeating the same command before resume rechecks live identity/access and
   returns the same accepted change set. A changed diff or conflicting state
   fails closed. A crash before the atomic transition leaves the project blocked;
   repeating recovery reuses matching persisted validation. Restarting the
   service leaves this recovered project paused. Only a **later, explicit owner
   resume through the existing authorised project command** permits lifecycle
   scheduling of one trusted commit continuation. Do not resume as part of this
   deployment or recovery procedure.

For permission/validation rejection, preserve the output and fix the diagnosed
host or security issue through its supported procedure. Do not widen permissions
outside the worktree or force workflow state to make recovery pass.

### Regression verification

Run the full credential-free suite and required static checks:

```sh
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
bash -n scripts/host/syntra-codex-launch
bash -n scripts/host/setup-codex-worker.sh
```

On a disposable Linux root host with `acl`, `python3`, `setpriv`, and pytest:

```sh
python -m pytest tests/integration/test_codex_filesystem_handoff.py -q
```

That suite executes the actual launcher and descriptor-based helper with real
separate users, restrictive worker-created source, effective read/traverse
checks, nonzero exits, SIGTERM/SIGKILL, repeated cleanup, masked ACLs, protected
Git metadata, escaping `.git`/source symlinks, hard links and injected ACL failure.
Ordinary non-root runs skip these tests; CI runs them explicitly as root.
`test_m32_handoff_recovery.py` covers recovery rejection, history/budget
preservation, interrupted atomic transitions, idempotent validation, 3,000 paused
ticks, restart, and exactly one continuation after fixture-only explicit resume.

## Confirmed causes and limits of the evidence

`LocalCodexCliRunner` correctly records **process** success for exit code zero.
The defect was `InitialCodexExecutor` treating that as implementation completion,
without observing the effective worktree. Model statements cannot establish
completion. The runner's `SUCCEEDED` evidence remains immutable and means only
that the process completed successfully.

`LifecycleCoordinator` previously deduplicated only active jobs. After a permanent
validation failure it created a new job, each with its own three-attempt budget.
This also affected initial coding failures. No bounded validation rework handoff
was implemented in that executor.

The previous root-owned host launcher delegated sandbox and feature selection to
worker defaults/configuration by executing plain `codex exec -`. The supplied Pi
stderr proves that its effective session was read-only and Code Mode required a
missing `/usr/local/bin/codex-code-mode-host`. The Pi configuration, CLI version
and feature-rollout records were not supplied; the exact source enabling Code
Mode cannot be established from this repository. Do not claim that an inspected
Pi configuration or a verified compatible ARM64 tool-host release exists.

## Application behavior

- Successful process evidence is retained separately from implementation evidence.
  Trusted M19 identity checks run again after execution and on replay. The M21
  collector observes the effective filesystem against the trusted HEAD, including
  untracked files and staged state. An empty result blocks the milestone and
  project, with `codex-empty-implementation`. Prose is never parsed as authority.
- Architect review rework uses the same post-execution and replay identity and
  effective-delta checks against the reviewed head. Empty output blocks with
  `codex-empty-review-rework`; unchanged rejected output also blocks. Terminal
  review jobs cannot be replaced with an original IMPLEMENT job.
- Normal first implementation still requires a clean READY worktree. Durable
  successful replay permits existing uncommitted output and never discards it.
- A correctable validation rejection transitions `VALIDATING_CHANGES -> CODING`
  and atomically queues one correction bound to its immutable validation ID.
  Initial implementation uses `CODEX_RUN`; Architect review corrections retain
  `CODEX_REVIEW_REWORK`, the authoritative rework task and original findings,
  and exact review/PR/head binding.
  Findings and remediation are reconstructed from durable evidence as prior-run
  feedback alongside the authoritative implementation or review-rework task and
  approved AGENTS. The
  original uncommitted work is retained. An unchanged rejected diff blocks rather
  than creating another identical validation/rework loop.
- Identity/history/escape failures block for human intervention. Secret/protected
  path findings remain durable, block acceptance, and may be corrected by removing
  the offending content; security findings are not waived. Infrastructure errors
  use existing classified scheduler retries on the same job. Automatic coding
  jobs have one infrastructure attempt because partial output cannot be blindly
  replayed as a clean implementation.
- The configured `retries.codex_cycle_limit` counts all persisted Codex invocations
  for the milestone, across job identities, infrastructure attempts and review
  rework. Historical runs count; recovery never resets that budget.
- Automatically scheduled coding/validation jobs carry the latest authoritative
  milestone transition ID as `lifecycle_generation`. Terminal work consumes that
  state entry's budget. Legacy jobs without this field are reconciled against the
  state-entry timestamp; ambiguous legacy evidence blocks conservatively.
  A legitimate new state entry can schedule subsequent work. Active-job checks
  and enqueue decisions are made inside one SQLite transaction.
- Replaying a completed rejection handoff does not save another change set or
  enqueue another coding job. Restart during the gap between validation evidence
  and its handoff reuses the same correlated exact-diff evidence.
- Blocker reasons and correlated structured events distinguish empty completion,
  execution prerequisite failures, validation rework, budget exhaustion and human
  intervention. Blocked status uses the persisted blocker rather than stale
  activity such as `Git running`. Raw prompts/provider output and credentials are
  not copied into these events.

No schema migration, data rewrite, automatic resume, provider invocation or
historical cleanup is part of application upgrade. Existing jobs, attempts,
findings, branches, run logs and transition histories remain available.

## Host deployment is separate

The service-side invocation remains:

```text
sudo -n /usr/local/libexec/syntra-codex-launch <exact-managed-worktree> <approved-codex> exec -
```

Previously, after identity isolation, the host executed:

```text
<approved-codex> exec -
```

The reviewed launcher now executes, after its prerequisite checks:

```text
<approved-codex> exec --ignore-user-config --sandbox workspace-write -c 'approval_policy="never"' --enable code_mode --enable code_mode_only --enable code_mode_host -
```

This explicitly retains Code Mode's fail-closed execution requirement. It does
not disable that requirement or enable unrestricted sandboxing. Worker user
configuration is ignored; authentication still uses the worker home. Review
model/provider defaults before deploying this profile. The profile requires a
CLI supporting all these options and feature names.

The CLI inspected in the development environment reports
`codex-cli 0.159.0-alpha.3`; its local help and feature catalog support this
invocation. This is **not** a verification of the Pi CLI or a compatible Pi
Code Mode host. There is no verified ARM64 tool-host artifact/version in the
incident evidence. Host deployment is gated until the operator obtains a
version-compatible CLI/host pair from the same trusted Codex distribution,
checks that the default tool-host path is `/usr/local/bin/codex-code-mode-host`,
and verifies that pair on the target ARM64 host. Do not download a similarly
named arbitrary executable, invent an installation URL, or downgrade security
features to get past this gate. An upstream distribution that cannot supply the
matching host is an unresolved provisioning prerequisite.

Installation requirements for the verified pair:

1. Approved CLI at `/usr/bin/codex` or `/usr/local/bin/codex`; matching tool host at
   `/usr/local/bin/codex-code-mode-host`. Files must be root owned, executable and
   neither group/other writable nor setuid/setgid. Keep package/runtime
   dependencies and containing directories protected from the worker as well.
2. Record the **exact** CLI version, vendor host version/build identity, ARM64
   provenance and independently tested compatibility in the host deployment
   record. Perform an operator-controlled disposable-workspace smoke check of
   Code Mode, workspace write access and Git-metadata denial; never use the
   paused application project for this check. No such paid check runs on upgrade.
3. After verifying that pair, create the root-owned, mode 0644 manifest
   `/etc/syntra-build/codex-runtime.sha256` with exactly two `sha256sum` records,
   one for the actual approved CLI path and one for the tool-host path. Its
   hashes must identify the verified pair. Replacing either binary requires
   renewed compatibility review and a new manifest. This is an operator
   attestation of compatibility, not an assertion that two hashes prove it.
4. Deploy the reviewed `setup-codex-worker.sh` helpers separately. This installs
   the runtime/access checks and launcher; it does not install Codex or invent
   the compatibility manifest. Run the offline diagnostic as the service user:

   ```sh
   sudo -n /usr/local/libexec/syntra-codex-launch --check-runtime /usr/local/bin/codex
   ```

The launcher checks the manifest, binary ownership/modes and supported CLI
options before any expensive execution. Missing prerequisites exit 66, recorded
as `codex-execution-environment-unavailable` in the application result.

The independent Linux boundary remains `setpriv` to `syntra-codex` with no new
privileges, a cleared environment, process/file limits, a global isolation lock,
project-specific temporary ACLs, read-only managed Git metadata, and cleanup.
After grants, the same worker identity reads the approved documents, creates and
removes one worktree write probe, and confirms Git metadata is not writable.
The Codex sandbox additionally scopes tool writes to the assigned workspace.
This is no permission for commit, push, PR creation or control-plane access.

## Recovery of m32-fx-reconciler

Keep the project paused. Do not use manual SQLite updates or delete its 2,825
historical validation failures.

1. Take and verify an operator backup using the existing `backup` and
   `restore-verify` commands. Preserve protected artifacts and the worktree too.
2. Deploy this application PR separately from the verified host runtime/profile.
   Keep the project paused throughout. Application startup performs no paid
   implementation attempt for a paused project.
3. Inspect `status`, `reconcile`, the protected original run logs, and the managed
   worktree. Ensure any stale active jobs have completed or been reconciled via
   M27. Confirm approved documents, expected repository/branch/HEAD, no unresolved
   security events, and the same empty effective diff.
4. As the trusted local operator, explicitly invoke:

   ```sh
   syntra-build-admin recover-empty-implementation \
     b1fde722-ecd7-46a6-91c2-f0f6e69d193a \
     c7c688c3-4844-4def-982f-017483ff5b12
   ```

   The command requires project `PAUSED` with resume state `BUILDING`, milestone
   `VALIDATING_CHANGES`, approved documents/task, no active jobs/security blockers,
   successful historical process evidence, an identity-verified empty worktree,
   matching historical empty validation and remaining Codex cycle budget. It
   uses the existing guarded state-transition repository to enter `CODING` and
   atomically queues one feedback-bound attempt. It leaves the project paused,
   makes no network/provider call, and is idempotent. IDs, documents, workspace,
   branch and historical evidence are preserved. Non-empty partial code is
   rejected for this narrow recovery; reconcile it without erasing it.
5. Review status and the queued recovery job. Only after verified host setup and
   an explicit owner decision, use the existing authorised project-resume command.
   This is a later operator action, not an upgrade step. If the new process
   produces no changes or the budget is exhausted, the workflow blocks again;
   recovery does not grant unlimited replacement attempts.

A project already blocked rather than paused is outside this command's narrow
legacy route. Resolve its persisted blocker through existing authoritative
recovery mechanisms; do not force state or reset counters in SQLite.

## Validation and review

Credential-free regressions cover actual effective changes, prose-independent
empty completion, missing/changed tool hosts, incompatible CLI options, effective
read-only OS access, 3,000 repeated ticks in both states, stable empty evidence,
restart after a handoff, effective-delta rework, exhaustion, and idempotent
operator recovery with history/documents/worktree preservation. The existing
M19–M32 acceptance and restart tests remain part of the full offline suite.

Review the host/profile gate separately from source correctness. Local tests
cannot establish Pi runtime compatibility or human approval. Nothing here
merges, deploys or resumes the live project.
