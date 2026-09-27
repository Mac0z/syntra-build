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

`RecoveryServices` wires the coordinator to the existing M22 PR/CI handoff, M23
`CIMonitor`, M19 `WorkspaceService`, M25 human intervention, and M26 `Gatekeeper`.
PR recovery reloads accepted ChangeSet/commit/intent identity and runs M22's
find-before-create lifecycle. CI recovery invokes the exact-head monitor. Merge recovery
has a dedicated GET-only Gatekeeper path: it performs two independent observations and
never exposes merge `PUT`. Without a configured conclusive service, mutation-bearing
states block, so restart cannot itself repeat `create` or merge. Commit recovery adopts
only an exact clean trusted commit. Push recovery uses the existing compare-before-push
implementation and never force-pushes.

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
# Observe/reconcile PR, CI and merge plus local Git/worktree state.
python -m syntra_build.m27_smoke --mode github \
  --database /path/to/m27-acceptance.db --data-root /configured/data/root

# Reconcile only commit/push and lost Codex/worktree state.
python -m syntra_build.m27_smoke --mode local \
  --database /path/to/m27-acceptance.db --data-root /configured/data/root

# Restore persisted human gates/interactions without a GitHub connection.
python -m syntra_build.m27_smoke --mode human \
  --database /path/to/m27-acceptance.db

# Compose every concrete recovery service in one bounded pass.
python -m syntra_build.m27_smoke --mode all \
  --database /path/to/m27-acceptance.db --data-root /configured/data/root
```

The command uses real SQLite state and composes the normal trusted host configuration,
GitHub adapters, workspace service, CI monitor, Gatekeeper and human-intervention
service selected by the mode. It prints concise append-only outcomes. Use only a copied
database and harmless acceptance repository. It does not reboot the host, start a
daemon, replay a merge, reset a worktree, or manufacture expected terminal states.
