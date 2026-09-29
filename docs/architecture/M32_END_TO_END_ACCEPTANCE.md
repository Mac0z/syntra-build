# M32 End-to-End Generated-Project Acceptance

M32 acceptance uses a **new, isolated database and configuration**. Never reuse the
historical development or M30/M31 acceptance database.

## Preparation

1. Create an M32-specific configuration and data root, owned by `syntra-build`.
2. Configure the real Telegram bot/authorised numeric user, GitHub App, and OpenAI
   Architect credentials through the established credential files. Do not place
   credentials in source, Telegram, prompts, or generated repositories.
3. Configure Codex as exactly `/usr/bin/codex` or `/usr/local/bin/codex`. Retain the
   M31 sudo helper, root-owned validation, temporary project ACL, `prlimit`,
   `setpriv syntra-codex --no-new-privs`, and `env -i` chain.
4. Start the service and verify schema `27` (`027_m32_orchestration`), health,
   metrics, backup creation, and recovery completion before ordinary dispatch.
5. Use a public generated repository unless the approved Telegram design explicitly
   requests private visibility.

## Acceptance sequence

From Telegram, create a small project with at least two sequential milestones and a
requirement likely to prompt one harmless first-review refinement. Answer only exact
design replies and legitimate gates. Approve the generated SPEC/AGENTS package, then
observe repository provisioning, task generation, Codex, validation, trusted
commit/push, one PR per milestone, CI, Architect review, verified merge, next
milestone activation, and final project completion.

While `CI_RUNNING`, restart the systemd service. Verify `RECOVERING` then `HEALTHY`,
the same project/milestone, PR number, branch, and head SHA, resumed CI monitoring,
and no duplicate PR or merge. Exercise one genuine CI rework or Architect
`CHANGES_REQUIRED` cycle and verify the same PR receives a new trusted head followed
by fresh CI and Architect approval.

Query status during design, CI, between milestone merges, after restart, and at
completion. Queries must not change state or interrupt workers.

## Prohibited operator actions

Do not run Git commands for the generated project, push, create/edit/merge PRs,
edit the generated repository, manipulate SQLite, manually trigger CI to advance
state, or point the run at the production Syntra source installation. Any such
rescue means M32 has not passed. M33 self-hosting is explicitly out of scope.

## Evidence to retain

Retain redacted health/metrics and backup evidence; Telegram design/gate/status
evidence; approved document identities; verified repository identity; both complete
milestones and transition histories; Codex, accepted change-set, commit, final CI,
final-head Architect approval, and verified merge evidence; the historical rework
verdict; same-PR proof; restart recovery observations; no unresolved gate; and no
active blocking security event. Never include secrets in the acceptance record.
