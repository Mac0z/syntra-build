# M32 empty implementation and terminal scheduling repair

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
- Normal first implementation still requires a clean READY worktree. Durable
  successful replay permits existing uncommitted output and never discards it.
- A correctable validation rejection transitions `VALIDATING_CHANGES -> CODING`
  and atomically queues one `CODEX_RUN` bound to its immutable validation ID.
  Findings and remediation are reconstructed from durable evidence as prior-run
  feedback alongside the accepted IMPLEMENT task and approved AGENTS. The
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
