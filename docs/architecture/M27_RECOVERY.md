# M27 startup recovery

Startup owns a three-stage application lifecycle: `STARTING`, `RECOVERING`, and
`READY`. `RecoveryCoordinator` enters the existing scheduler drain before discovery,
runs a drained scheduler cycle, and releases dispatch only after the recovery run is
durably completed. A global persistence failure leaves the scheduler drained.

Discovery reads non-terminal projects, active milestones, and non-terminal jobs from
SQLite. Every decision is appended to migration 025's `recovery_runs` and
`recovery_observations`; observations cannot be updated or deleted. A failure for one
project is converted to a project-scoped blocked disposition while discovery continues.

## Reconciliation policy

State handlers are explicit observation-first seams for PR, CI, Git, merge, and review
services. They receive project-scoped durable identity and cannot obtain another
project's subject from discovery. PR and merge recovery have no default mutation path:
without a conclusive handler they block, so restart can never itself repeat `create` or
merge `PUT`. CI and Architect work retain their existing external wait until their
observer reconciles exact-head evidence. Commit/push handlers must compare persisted
commit intent, local HEAD, and remote ref; force-push and uncertain second commits are
not recovery actions.

Human states restore the existing durable gate and preserve notification, response,
human-test SHA/CI binding, and feedback-interaction state. Recovery does not create or
notify a second gate. Deployments can invoke the existing idempotent
`HumanInterventionService.reconcile_review()` handler for an interrupted resolved-review
handoff.

Local `DISPATCHED`/`RUNNING` Codex, Git, or Architect executions have no surviving
in-process worker. Recovery marks the old job and running attempt `ABANDONED`, preserves
the worktree, and queues one correlated replacement only when an injected workspace
inspection proves it clean. Dirty, missing, or ambiguous worktrees are preserved and do
not receive replacement work. Repeating recovery therefore cannot reuse an attempt or
duplicate a replacement.

## Host acceptance

Run the bounded, non-daemon inspection against a copied or purpose-built database:

```bash
python -m syntra_build.m27_smoke --database /path/to/m27-acceptance.db
```

The command uses real SQLite state, performs coordinator discovery/reconciliation, and
prints concise append-only outcomes. Provider observation handlers may be supplied by a
host harness for a harmless GitHub acceptance repository; destructive actions should
remain observation-only. It does not reboot the host, start a daemon, replay a PR
creation/merge, reset a worktree, or manufacture expected terminal states.
