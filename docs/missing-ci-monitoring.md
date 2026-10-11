# Bounded monitoring when required GitHub Actions checks are absent

Issue #73 Deliverable B extends M23 CI monitoring. Each observation still applies
to one verified pull request and its exact current head SHA. Required CI must
pass before Architect review can begin; absent checks never supply passing CI
evidence.

## Grace policy

A successful GitHub observation with absent required checks enters a bounded
startup grace period. This allows delayed workflow startup without classifying
the delay as a test failure. Syntra persists the first missing observation's UTC
time, its deadline, and the number of successful missing observations for that
exact PR/head. The deadline is not recomputed after restart or configuration
changes. A different head starts a separate grace period.

Configure these ordinary settings in the central `ci` group:

```json
{
  "ci": {
    "missing_grace_seconds": 600,
    "missing_max_observations": 20
  }
}
```

The corresponding environment variables are `SYNTRA_CI_MISSING_GRACE_SECONDS`
and `SYNTRA_CI_MISSING_MAX_OBSERVATIONS`. Explicit configuration takes precedence.
The grace must be finite, greater than zero, and at most 86,400 seconds (one day).
This operational upper bound prevents overflowing deadlines and excessively long
startup waits. The observation limit must be a positive integer. Syntra blocks
when either bound is reached. Known queued or running required checks continue
through the existing adaptive polling policy. An independently head-bound,
applicable workflow run reported as queued or running also provides observable
startup progress while its jobs are not yet visible and uses adaptive polling.
That startup observation supplies no passing check evidence. A completed workflow
run with no required jobs remains subject to the bounded missing-check policy.

Existing `UNKNOWN` records initialise this grace on the next successful missing
observation after upgrade. A GitHub API error does not create evidence of absent
checks and follows the existing classified, bounded infrastructure retry policy.
Real failed tests continue through the ordinary implementation rework path.

Workflow discovery reads `.github/workflows` at the monitored head SHA. A missing
directory response counts as absence only after a readable root listing proves
Contents access at that same head. Permission errors and malformed responses
remain provider failures rather than evidence of missing workflows.

## Operator diagnosis and recovery

During grace, the durable diagnosis identifies required checks still absent for
the exact PR/head and the grace deadline. After expiry or observation exhaustion,
Syntra records an actionable blocking diagnosis and transitions the milestone
from `CI_RUNNING` to `BLOCKED` instead of scheduling indefinite `UNKNOWN` polls.
The milestone retains `resume_state=CI_RUNNING`. No Codex rework is requested for
missing CI infrastructure.

Inspect the named repository, pull request, head SHA, and required checks. Check
that a trusted workflow exists and its triggers, branch/path filters, permissions,
Actions enablement, runner availability, and quota allow it to produce the
required checks. GitHub API access failures are reported separately. Any workflow
repair requires an independently approved trusted operational change; the coding
agent cannot relax required checks or create a placeholder success.

After resolving the cause, normal trusted controls must explicitly unblock the
milestone to its persisted recovery target, `CI_RUNNING`. The existing guarded
control-plane mechanism is a `MilestoneTransitionRequest` applied through
`SQLiteMilestoneRepository.apply_transition`; it verifies the expected blocked
state and the persisted recovery target and records the transition atomically.
This change adds no operator unblock command. A project resume command only
restores a `PAUSED` project and does not unblock its milestone. The terminal
`BLOCKED` milestone remains blocked until that explicit trusted transition;
restart or new GitHub activity alone does not clear it. Trusted recovery must also
arrange fresh CI reconciliation; the terminal record has no scheduled poll.
Syntra requires fresh evidence for the current PR/head. All normal Architect, human,
and Gatekeeper review requirements remain in force.

`PAUSED` projects receive no CI polling or new rework. Pause preserves the grace
deadline and counter. When a project was paused while its milestone was still
`CI_RUNNING`, project resume permits a fresh provider observation: delayed checks
that have appeared can proceed normally, while checks still absent after their
deadline produce the blocking diagnosis. Pause/resume does not clear an already
`BLOCKED` milestone. CI waits hold no Codex capacity and leave other projects'
scheduling independent.

## Migration and scope

Migration `030_bounded_unknown_ci` adds nullable UTC grace timestamps and a bounded
observation counter to existing CI run records. It preserves prior observations
and transitions and does not manufacture CI evidence. Back up the database before
upgrade. Migrations are forward-only; rollback requires the previous application
version and its pre-migration backup, rather than editing evidence or running an
older daemon against the upgraded database.

This change does not retrofit workflows or modify the live
`m32-fx-reconciler` project. The trusted bootstrap is documented in
[Trusted CI bootstrap](trusted-ci-bootstrap.md); issue #73 Deliverable C remains
separate work.
