# Syntra Build — Operations

## 1. Purpose

This document defines the operational model for Syntra Build.

It describes how the system is:

- deployed;
- started and stopped;
- monitored;
- scheduled;
- rate-limited;
- backed up;
- recovered;
- upgraded;
- maintained;
- diagnosed;
- operated safely on the Syntra Raspberry Pi host.

This document builds on:

- `VISION.md`
- `WORKFLOW.md`
- `STATE_MACHINE.md`
- `ARCHITECTURE.md`
- `INTERFACES.md`
- `DATA_MODEL.md`
- `SECURITY.md`

The operational design assumes that Syntra Build is a long-running orchestration service and that GitHub Actions performs the majority of heavyweight and cross-platform build work.

---

# 2. Operational Goals

The operational model must ensure that:

1. Syntra Build can run continuously on the Syntra host;
2. multiple projects can progress independently;
3. Raspberry Pi resource limits are respected;
4. waiting projects consume minimal local resources;
5. failures are retried predictably;
6. repeated failures do not create infinite loops;
7. restart recovery is automatic and safe;
8. project state survives host and service restarts;
9. active work can be queried at any time;
10. backups protect unique Syntra state;
11. upgrades do not lose project progress;
12. operational failures are visible through logs, metrics and messaging;
13. one broken project cannot destabilise all other projects.

---

# 3. Initial Deployment Model

Syntra Build should initially run directly on the Syntra host as a managed system service.

Preferred structure:

```text
Syntra host
│
├── syntra-build service
├── syntra-codex execution identity
├── SQLite database
├── local Git repositories/worktrees
├── local artifact store
├── Git CLI
├── Codex CLI
├── Prometheus metrics endpoint
└── systemd
```

The initial deployment should avoid unnecessary distributed infrastructure.

---

# 4. Service Management

## 4.1 Preferred supervisor

Syntra Build should run under `systemd`.

The service unit should provide:

- automatic startup after boot;
- restart on unexpected failure;
- graceful shutdown;
- controlled environment;
- credentials loading;
- resource limits;
- structured logging integration where practical.

---

## 4.2 Service identity

The main service runs as the dedicated:

```text
syntra-build
```

user.

Codex execution runs separately as:

```text
syntra-codex
```

as defined in `SECURITY.md`.

---

# 5. Startup Sequence

Syntra Build startup must occur in a defined order.

Recommended sequence:

```text
1. Load configuration
2. Load credentials
3. Verify filesystem paths
4. Open database
5. Verify schema version
6. Run required migrations
7. Perform integrity checks
8. Initialise external adapters
9. Enter recovery mode
10. Reconcile incomplete projects/jobs
11. Start messaging intake
12. Start scheduler
13. Start CI reconciliation loop
14. Expose ready/healthy state
```

Privileged workflow execution must not begin before recovery has completed.

---

# 6. Graceful Shutdown

On normal shutdown, Syntra Build should:

1. stop accepting new schedulable work;
2. mark service as draining;
3. stop dispatching new Codex and Architect jobs;
4. allow short atomic operations to complete where safe;
5. terminate or abandon long-running Codex work according to policy;
6. persist current job states;
7. flush database writes;
8. record graceful shutdown time;
9. close external sessions;
10. exit cleanly.

GitHub Actions already running should normally be left running remotely.

They can be reconciled after restart.

---

# 7. Crash Recovery

Unexpected process termination must not corrupt workflow state.

Because state is persisted before side effects, restart should be able to reconcile safely.

Jobs found in:

```text
DISPATCHED
RUNNING
WAITING_EXTERNAL
```

must be inspected.

Depending on actual external state, they may become:

```text
SUCCEEDED
WAITING_EXTERNAL
ABANDONED
RETRY_WAIT
FAILED
```

---

# 8. Host Reboot Recovery

A host reboot is treated similarly to service crash recovery.

On restart Syntra should inspect:

- local worktrees;
- active Codex job records;
- open GitHub PRs;
- GitHub Actions state;
- outstanding human gates;
- queued retries;
- incomplete merge operations.

No project should need to be manually reconstructed from memory.

---

# 9. Scheduler Model

The scheduler manages units of work across all projects.

Jobs are scheduled according to:

- eligibility;
- project state;
- worker type;
- priority;
- concurrency limits;
- retry timing;
- fairness.

---

# 10. Initial Concurrency Limits

Recommended initial defaults:

```text
Architect jobs:              2 concurrent
Codex jobs:                  2 concurrent
Repository provisioning:    1 concurrent
Git push operations:         2 concurrent
PR creation operations:     2 concurrent
Merge operations:           1 concurrent
CI reconciliation:          many lightweight
Messaging operations:       many lightweight
Human waits:                 unlimited
```

These should be configurable rather than hard-coded.

---

# 11. Raspberry Pi Resource Policy

The Pi should function primarily as an orchestrator.

It should not become a general build farm.

Local workload should focus on:

- Git operations;
- AI orchestration;
- lightweight tests;
- Codex CLI execution;
- project worktree management;
- persistence;
- messaging;
- monitoring.

Heavy work should normally run in GitHub Actions.

---

# 12. CPU Policy

Codex processes may consume significant CPU while running local commands.

Operational controls should include:

- maximum Codex concurrency;
- optional CPU quota;
- process priority;
- fair scheduling between projects.

The service itself should remain responsive even when Codex workers are busy.

---

# 13. Memory Policy

Memory pressure should be monitored.

If available memory falls below a configured safety threshold:

- do not start new Codex jobs;
- allow lightweight status queries;
- continue CI monitoring;
- continue messaging;
- notify the operator if pressure persists.

The scheduler should resume Codex dispatch automatically once memory returns above the threshold.

---

# 14. Disk Policy

Disk space is particularly important because local worktrees and build artifacts can grow.

Syntra should monitor:

- database size;
- project repository size;
- worktree size;
- artifact size;
- available filesystem space.

Recommended thresholds should include:

```text
warning threshold
dispatch-stop threshold
critical threshold
```

Example policy:

```text
< 20% free → warning
< 10% free → stop new Codex jobs
< 5% free  → critical operational alert
```

Exact thresholds should be configurable.

---

# 15. Worktree Cleanup

Milestone worktrees should be removed only after:

- PR merge verified;
- milestone marked complete;
- no unresolved recovery operation exists.

Cleanup should include:

- local worktree removal;
- stale branch pruning where safe;
- temporary file cleanup.

Historical source remains available in GitHub.

---

# 16. Artifact Retention

Artifact storage may include:

- Architect raw responses;
- Codex stdout/stderr;
- CI excerpts;
- human-test attachments;
- temporary logs.

Initial policy should favour preserving diagnostic information while keeping storage bounded.

A later configurable retention policy may distinguish:

```text
audit-critical
diagnostic
temporary
reproducible
```

Only reproducible or temporary artifacts should be eligible for automatic deletion.

---

# 17. Job Priority

Suggested job priority levels:

```text
CRITICAL
HIGH
NORMAL
LOW
```

Example usage:

### CRITICAL

- recovery reconciliation;
- security response;
- merge verification after uncertain outcome.

### HIGH

- human gate response processing;
- project resume;
- failed privileged operation reconciliation.

### NORMAL

- Codex runs;
- Architect tasks;
- PR creation;
- CI evaluation.

### LOW

- background cleanup;
- old artifact pruning;
- historical metrics processing.

---

# 18. Fair Scheduling

One project must not monopolise available workers.

Initial scheduling should use a simple fairness strategy such as:

```text
round-robin between eligible projects
```

with per-project limits.

A project repeatedly cycling through rework should return to the queue rather than immediately reacquiring the same worker indefinitely.

---

# 19. One Active Milestone per Project

Version one should execute at most one implementation milestone per project at a time.

This simplifies:

- Git branch handling;
- PR ownership;
- recovery;
- specification change management;
- human testing;
- state reporting.

Parallelism occurs across projects instead.

---

# 20. Waiting States

Projects waiting for:

- human input;
- GitHub Actions;
- retry delay;
- external API response;
- project pause

should not consume scarce worker capacity.

This is central to supporting many concurrent projects on the Pi.

---

# 21. Retry Strategy

Retries are divided into two categories:

1. infrastructure retries;
2. workflow rework cycles.

These must remain separate.

---

# 22. Infrastructure Retry Policy

Examples:

- Telegram timeout;
- GitHub API timeout;
- OpenAI API timeout;
- transient network failure;
- temporary Git push failure.

Recommended default retry pattern:

```text
attempt 1
wait 5 seconds

attempt 2
wait 30 seconds

attempt 3
wait 2 minutes

attempt 4
wait 10 minutes
```

Exact values should be configurable.

Exponential backoff with jitter is preferred.

---

# 23. Workflow Rework Limits

Implementation failures require different handling.

Suggested initial defaults:

```text
Codex implementation attempts:        5
CI rework cycles:                     5
Architect review rework cycles:       5
Human-test rework cycles:             configurable / bounded
```

These values should not be treated as permanent architectural constants.

---

# 24. Retry Exhaustion

When retry limits are exhausted, Syntra must not continue indefinitely.

The affected milestone should normally enter:

```text
BLOCKED
```

and the user should receive a concise message explaining:

- what failed;
- how many attempts occurred;
- what Syntra believes is blocking progress;
- whether human action is required.

Other projects continue normally.

## 24.1 M13 policy implementation

Infrastructure attempts use the durable job attempt number and the individual job's
maximum. The configured default is four attempts. Delays use the configurable
`5, 30, 120, 600` second schedule with bounded multiplicative jitter (20 percent by
default); the jitter source is injected for deterministic testing. `next_retry_at` is
persisted in UTC, and each scheduler cycle promotes due retries before ordinary queue
selection. No worker capacity is reserved while a job is in `RETRY_WAIT`.

Milestone Codex cycles and CI, Architect, and human-test rework cycles are separate
explicit counters. Their configured limits mean permitted logical cycles; after that
many have been recorded, the next requested cycle blocks the milestone rather than
starting an additional cycle. The initial human-test limit is five, matching the other
bounded rework defaults until a project-specific policy is introduced.

Within each worker class, higher numeric priority tiers are always considered first.
Within one priority tier, queued work is round-robin by project, taking one job from
each project per pass. An in-process cursor starts the next cycle after the project
that most recently received a dispatch. The cursor is deliberately not durable: a
service restart resets only scheduling order and cannot duplicate or corrupt a durable
job claim.

---

# 25. CI Monitoring

The CI Monitor should periodically reconcile active PRs in:

```text
CI_RUNNING
```

state.

Polling should be efficient.

An initial interval around tens of seconds is likely sufficient, with adaptive slowing for long-running jobs.

Exact values should be configurable.

---

# 26. CI Polling Strategy

Suggested behaviour:

```text
first few minutes:
poll relatively frequently

long-running CI:
reduce polling frequency

completed:
stop polling
```

Webhooks may replace polling later if operationally worthwhile.

Version one should favour simplicity.

---

# 27. Architect API Operations

Architect calls should:

- use explicit timeout;
- record provider request ID;
- record model;
- capture structured result;
- retry transient failures;
- reject malformed responses;
- preserve failed response metadata where useful.

Architect failure must never silently produce approval.

---

# 28. Codex Execution Operations

Each Codex run should record:

- process ID;
- project;
- milestone;
- attempt number;
- start time;
- timeout;
- worktree;
- task type;
- completion status;
- output references.

Codex processes that exceed timeout should be terminated safely.

The full process tree should be stopped rather than leaving child processes behind.

---

# 29. Codex Timeout Defaults

Different project types may need different timeouts.

A configurable initial default may be:

```text
60 minutes
```

The Architect task or project specification may allow a longer explicit timeout where justified.

Timeout should produce:

```text
TIMED_OUT
```

rather than being treated as successful or silently abandoned.

---

# 30. System Health States

Syntra Build should expose operational health separate from project state.

Suggested states:

```text
STARTING
RECOVERING
HEALTHY
DEGRADED
DRAINING
UNHEALTHY
```

---

# 31. HEALTHY

The service is operational and able to schedule work.

Expected conditions include:

- database healthy;
- required filesystem paths accessible;
- scheduler running;
- messaging operational or reconnecting within normal tolerance;
- no system-wide blocking condition.

---

# 32. DEGRADED

The service remains usable but one capability is impaired.

Examples:

- GitHub temporarily unreachable;
- Architect provider unavailable;
- disk warning threshold reached;
- Telegram delivery failing.

Projects unaffected by the impaired component may continue.

---

# 33. UNHEALTHY

The service cannot safely perform normal autonomous workflow.

Examples:

- database integrity failure;
- disk critical threshold;
- required filesystem unavailable;
- unsupported schema version;
- credential failure affecting all privileged GitHub operations.

Read-only status may remain available where practical.

---

# 34. Health Endpoint

Syntra Build should expose a local health endpoint.

Suggested endpoints:

```text
/health
/ready
/metrics
```

### `/health`

Indicates whether the process is alive.

### `/ready`

Indicates whether normal workflow execution may proceed.

### `/metrics`

Prometheus-compatible metrics.

These endpoints should not expose sensitive project data.

---

# 35. Logging

Logs should be structured.

Recommended fields:

```text
timestamp
severity
component
project_id
milestone_id
job_id
gate_id
correlation_id
event
message
```

Human-readable console output may still be provided during development.

---

# 36. Log Levels

Suggested levels:

```text
DEBUG
INFO
WARNING
ERROR
CRITICAL
```

Normal production operation should default to `INFO`.

Verbose Codex or provider payloads should not be emitted directly into normal application logs.

---

# 37. Log Rotation

Logs must be rotated.

Possible implementation:

- Python rotating logs; or
- journald/systemd with retention policy.

The operational requirement is:

- no unbounded log growth;
- preserved recent diagnostics;
- no secret leakage.

---

# 38. Prometheus Metrics

Syntra Build should expose metrics compatible with the existing Syntra Prometheus/Grafana environment.

Suggested metrics include:

```text
syntra_build_projects_total
syntra_build_projects_active
syntra_build_projects_blocked
syntra_build_projects_waiting_human

syntra_build_jobs_queued
syntra_build_jobs_running
syntra_build_jobs_failed

syntra_build_codex_running
syntra_build_codex_attempts_total
syntra_build_codex_duration_seconds

syntra_build_architect_running
syntra_build_architect_requests_total
syntra_build_architect_duration_seconds

syntra_build_ci_running
syntra_build_ci_failures_total

syntra_build_github_api_errors_total
syntra_build_telegram_errors_total

syntra_build_state_transitions_total

syntra_build_disk_free_bytes
syntra_build_artifact_bytes
```

---

# 39. Grafana Dashboard

A future Grafana dashboard should show at minimum:

- Syntra Build health;
- active projects;
- blocked projects;
- projects waiting for human action;
- queued jobs;
- active Codex workers;
- Architect calls;
- CI waits;
- failure rate;
- Codex duration;
- disk space;
- host CPU;
- host memory.

The messaging platform remains the primary human project interface.

Grafana is for operational visibility.

---

# 40. Proactive Notifications

Syntra should proactively notify the human only when useful.

Recommended notifications include:

```text
design approval required
human decision required
human test required
project blocked
retry exhaustion
critical security issue
project completed
system-wide degradation requiring action
```

Routine successful internal transitions should not generate excessive noise.

---

# 41. Status Queries During Failure

Status queries should remain available wherever possible, even when a project is:

```text
BLOCKED
FAILED
PAUSED
WAITING_HUMAN
```

The response should explain:

- current state;
- most recent error;
- outstanding action;
- next possible recovery path.

---

# 42. Backup Strategy

Syntra Build should have automatic backups.

Backup scope includes:

```text
SQLite database
configuration
project documents not yet committed
human gate state
local unique artifacts
un-pushed local changes where practical
```

---

# 43. SQLite Backup

SQLite backups must use a database-safe backup mechanism.

The live database file should not simply be copied while assuming consistency.

Preferred approaches include:

- SQLite backup API;
- SQLite online backup command;
- safe snapshot mechanism.

---

# 44. Backup Frequency

Suggested starting policy:

```text
database backup: daily
additional backup before application upgrade
additional backup before schema migration
```

Given the modest database size, more frequent backups may be reasonable.

---

# 45. Backup Retention

Initial example:

```text
7 daily backups
4 weekly backups
3 monthly backups
```

The exact policy should be configurable.

---

# 46. Off-Host Backup

At least one backup copy should eventually exist outside the Syntra host.

Possible destinations include:

- existing network storage;
- encrypted remote storage;
- another trusted host.

Off-host backup design should protect project and messaging data appropriately.

---

# 47. Backup Verification

A backup is not considered reliable unless restore is periodically tested.

Operational checks should include:

- backup file exists;
- backup readable;
- database integrity check passes;
- schema version recognised.

Periodic test restore to a temporary location is recommended.

---

# 48. Recovery Priority

Recovery should prioritise preservation of workflow correctness over rapid automatic progress.

If Syntra cannot confidently determine whether an external side effect succeeded, it should:

1. inspect external state;
2. preserve evidence;
3. block the relevant project if ambiguity remains;
4. request human intervention if required.

It should not guess.

---

# 49. Disaster Recovery

If the Syntra host is lost, recovery should be possible from:

- latest database backup;
- configuration backup;
- credentials restored separately;
- GitHub project repositories;
- GitHub Syntra Build source repository.

Unpushed work since the last backup may be lost unless worktree backup captures it.

This risk should be minimised by committing/pushing milestone changes promptly after validation.

---

# 50. Upgrade Process

A normal upgrade should follow:

```text
1. Pause new job dispatch
2. Allow/stop active work safely
3. Create database backup
4. Stop Syntra Build service
5. Update application from the controlled GitHub repository or release
6. Install dependencies
7. Run database migrations
8. Run integrity checks
9. Start service in RECOVERING
10. Reconcile active work
11. Enter HEALTHY
```

---

# 51. Rollback

Application rollback should be possible only where the older version supports the current database schema/state-machine version.

If a migration is not backward compatible:

- rollback may require restoring the pre-upgrade backup;
- workflow progress after the migration would then be lost.

This should be documented in release notes.

---

# 52. Release Versioning

Syntra Build should use explicit application versions.

Suggested:

```text
semantic versioning
```

Example:

```text
0.1.0
0.2.0
1.0.0
```

The running version should be exposed in logs, health output and metrics.

---

# 53. Database Migration Operations

Before migrations:

- create backup;
- stop normal scheduler execution.

During migrations:

- no project jobs run.

After migrations:

- run integrity checks;
- record migration version;
- enter recovery before normal scheduling.

---

# 54. Configuration Management

Configuration should be loaded from a predictable controlled source.

Categories include:

```text
filesystem
database
telegram
architect
codex
github
scheduler
timeouts
retry limits
logging
metrics
backups
security thresholds
```

Configuration changes should not require source-code edits.

---

# 55. Configuration Validation

Startup must reject invalid configuration.

Examples:

- negative concurrency;
- missing required credential reference;
- invalid path;
- worktree root overlapping control-plane source;
- unsupported state-machine version.

Failing safely is preferable to operating with ambiguous configuration.

---

# 56. Runtime Configuration Changes

Version one does not require fully dynamic configuration reload.

A controlled service restart is acceptable for most operational configuration changes.

Human project commands such as pause/resume remain dynamic because they are workflow data rather than service configuration.

---

# 57. Operational Commands

A small administrative CLI may be useful for local host maintenance.

Potential commands:

```text
syntra-build status
syntra-build health
syntra-build projects
syntra-build reconcile
syntra-build backup
syntra-build database-check
syntra-build version
```

Normal project control should remain through Telegram.

The administrative CLI is for recovery and host operations.

---

# 58. Manual Reconciliation

An operator should be able to request reconciliation for:

- one project;
- one milestone;
- all projects.

Reconciliation should inspect state without immediately mutating external systems.

Example:

```text
syntra-build reconcile flowtrack
```

Potential output:

```text
Project state: BUILDING
Milestone: M4 / CI_RUNNING
PR #12 exists
Head SHA matches
GitHub Actions passed
Local state is stale
Recommended transition: ARCHITECT_REVIEW
```

---

# 59. Manual Overrides

Manual override capability should be deliberately limited.

An operator must not casually be able to:

```text
mark milestone COMPLETE
force merge
fake CI PASS
fake Architect APPROVE
```

Recovery actions should restore consistency, not bypass workflow controls.

If emergency override functionality is ever added, it must be explicit and audited.

---

# 60. Project Pause Operations

Pausing a project should:

- prevent new jobs;
- leave current external CI running;
- stop merge;
- preserve current workspace;
- preserve human gates;
- continue status queries.

The project can later resume after reconciliation.

---

# 61. Project Cancellation Operations

Cancellation should:

- stop future scheduling;
- terminate active Codex safely;
- cancel queued jobs;
- cancel human gates;
- preserve GitHub repository;
- preserve audit history;
- clean up temporary workspaces according to policy.

Cancellation is not deletion.

---

# 62. Blocked Project Operations

A blocked project should remain queryable.

Syntra should persist:

- reason;
- originating error;
- retry exhaustion information;
- recommended operator or human action;
- resume state.

A resolved blocking issue should allow explicit or automatic reconciliation back to the correct workflow state.

---

# 63. Project Failure Operations

`FAILED` should be reserved for conditions that cannot safely continue automatically.

Examples:

- irreconcilable repository identity mismatch;
- project-specific corruption;
- terminal policy violation.

System-wide problems should not arbitrarily mark every project failed.

---

# 64. System-Wide Failure

Examples:

- database unavailable;
- database integrity failure;
- critical disk exhaustion;
- invalid schema;
- core configuration failure.

The service should enter:

```text
UNHEALTHY
```

and stop privileged scheduling.

Read-only diagnostics should remain available where possible.

---

# 65. External Provider Outage

If an external service is unavailable:

## Telegram outage

- project jobs may continue;
- human gates cannot be delivered;
- status messages queue for retry.

## Architect outage

- Codex-independent waits continue;
- new Architect work queues;
- unrelated CI may continue.

## GitHub outage

- Codex may finish local work;
- no push/PR/merge;
- affected jobs retry.

## Codex provider outage

- Codex jobs queue or retry;
- Architect/status/CI work may continue.

This isolation is an important operational property.

---

# 66. Network Outage

During general Internet loss:

- Syntra preserves local state;
- no external privileged actions are assumed successful;
- active remote states become unknown until reconciliation;
- retries back off;
- status queries may report connectivity degradation;
- local project state remains intact.

After connectivity returns, reconciliation runs before workflow progression.

---

# 67. Power Loss

The system must tolerate abrupt power loss.

Key protections include:

- SQLite transactions;
- WAL mode;
- state persisted before side effects;
- Git's object model;
- startup reconciliation.

Uncommitted Codex workspace changes may survive filesystem recovery and should be inspected rather than discarded.

---

# 68. Database Maintenance

Routine maintenance may include:

- backups;
- integrity checks;
- checkpoint management;
- occasional vacuum where justified.

Maintenance tasks should run when the database is lightly loaded.

Database maintenance must never block for long enough to make the messaging interface appear dead without health reporting.

---

# 69. SQLite WAL Management

WAL size should be monitored.

Checkpointing should occur automatically or through SQLite normal behaviour.

Operational metrics may include:

```text
database size
WAL size
last backup age
last integrity check
```

---

# 70. Clock and Time

The host clock must remain synchronised.

Accurate time is important for:

- job scheduling;
- retries;
- state history;
- GitHub correlation;
- human gate timing;
- audit.

The operating system should use standard time synchronisation.

---

# 71. Correlation IDs in Operations

Every multi-step workflow should retain a correlation ID across:

```text
Architect request
Codex run
Git validation
commit
push
PR
CI
Architect review
human gate
merge
```

This greatly simplifies diagnosis.

---

# 72. Operational Diagnostics

For a project, diagnostics should be able to show:

```text
project state
milestone state
current activity
queued/running jobs
retry counters
workspace path
branch
PR
head SHA
CI state
latest Architect review
human gates
last errors
recent transitions
```

This should be available without manually querying SQLite.

---

# 73. Diagnostic Bundles

A future administrative command may generate a diagnostic bundle containing:

- recent project logs;
- state snapshot;
- recent transition history;
- job history;
- external IDs;
- configuration summary without secrets.

This would help diagnose complex failures without exposing credentials.

---

# 74. Performance Monitoring

Operational performance should track:

- project throughput;
- average milestone duration;
- Codex duration;
- Architect latency;
- CI wait time;
- number of rework cycles;
- scheduler queue time;
- GitHub API latency.

These metrics can later help optimise concurrency and retry policies.

---

# 75. Initial Capacity Expectations

Version one is intended for personal/small-scale concurrent development.

A reasonable design target is:

```text
dozens of known projects
several active projects
1–2 simultaneous Codex workers
multiple projects waiting on CI/human input
thousands of historical workflow records
```

The architecture should not optimise prematurely for hundreds of simultaneous Codex workers.

---

# 76. Resource Expansion

If Syntra Build eventually outgrows the Raspberry Pi, the architecture should permit:

- moving Codex execution to another host;
- moving persistence to PostgreSQL;
- adding worker hosts;
- introducing a proper message queue;
- adding self-hosted CI runners.

These are future scaling choices, not initial requirements.

---

# 77. Operational Security Checks

Periodic checks should confirm:

- service running as expected user;
- Codex has no privileged credentials;
- filesystem permissions intact;
- disk thresholds healthy;
- GitHub authentication valid;
- Telegram authentication valid;
- backup age acceptable;
- database integrity healthy.

---

# 78. Monitoring Alerts

Useful operational alerts include:

```text
Syntra service down
database unhealthy
disk below critical threshold
backup overdue
repeated GitHub failures
repeated Architect failures
repeated Codex failures
unexpected worker count
security event HIGH/CRITICAL
project blocked for extended period
```

Telegram may be used for operator alerts where appropriate.

---

# 79. Human Gate Reminders

Human gates should not silently disappear.

Optional reminders may be sent after configurable periods.

Example:

```text
24 hours
3 days
7 days
```

However:

- reminders must not imply urgency unless appropriate;
- silence never means approval;
- no destructive timeout occurs automatically.

---

# 80. Long-Running Paused Projects

Paused projects may remain paused indefinitely.

Syntra should not automatically cancel or delete them.

Periodic status summaries may optionally identify long-paused projects.

---

# 81. Completed Project Retention

Completed projects remain queryable.

Syntra should preserve:

- final project state;
- milestone history;
- PR references;
- Architect review history;
- human decisions;
- release/build references;
- audit trail.

Local worktrees may be removed.

---

# 82. Syntra Build Repository Operations

Syntra Build's own source is stored in GitHub.

This allows Syntra Build itself to be developed through the same controlled workflow used for other projects:

- Architect design/review;
- Codex implementation in a controlled worktree;
- Syntra-managed commits and pushes;
- GitHub pull requests;
- GitHub Actions;
- gated merge.

A failure affecting the Syntra Build repository should be isolated from unrelated project repositories wherever possible.

The running Syntra Build installation must not be modified directly by a Codex run. Deployment of merged Syntra Build changes follows the controlled upgrade process.

---

# 83. GitHub Operations

GitHub is the canonical source-control platform for Syntra Build itself and for generated projects.

Syntra should reconcile GitHub object IDs rather than rely solely on local Git state.

Operationally important GitHub objects include:

```text
repository
branch
PR
head SHA
CI runs/checks
merge result
release artifacts
```

---

# 84. Release Artifacts

Generated projects may create releases or build artifacts through GitHub Actions.

Syntra should store references to such artifacts when needed for:

- human testing;
- final completion;
- status messages.

Syntra should not copy large artifacts locally unless there is a reason.

---

# 85. Human Test Build Availability

Before issuing a human-test gate, Syntra should verify that the referenced artifact or preview is actually available.

A human gate should not be sent with a broken or incomplete download/preview reference.

---

# 86. Operational State versus Project State

System operational health and project workflow state are separate concepts.

Example:

```text
System: DEGRADED
GitHub temporarily unavailable

FlowTrack: BUILDING / PUSHING
PulseVault: WAITING_HUMAN / HUMAN_TEST
Website X: DESIGNING
```

The system can be degraded without marking all projects failed.

---

# 87. Recovery Ordering

After restart, reconciliation should prefer this order:

```text
1. System/database integrity
2. Security-critical ambiguous operations
3. Merge state
4. Repository/PR state
5. CI waits
6. Local Codex/worktree state
7. Retry queues
8. Human gates
9. Normal scheduling
```

Operations that could duplicate privileged side effects receive highest reconciliation priority.

---

# 88. Merge Recovery

If shutdown occurs during `MERGING`:

1. query GitHub PR;
2. determine whether merge occurred;
3. verify merge commit if present;
4. update local records;
5. continue to `MERGE_VERIFY` if appropriate;
6. never issue a second merge blindly.

---

# 89. PR Creation Recovery

If shutdown occurs during `PR_CREATING`:

1. query GitHub by expected branch;
2. locate existing open PR if present;
3. adopt and record it;
4. otherwise retry creation.

This prevents duplicate PRs.

---

# 90. Codex Recovery

If shutdown occurs during `CODING`:

1. check whether original process still exists;
2. mark lost process `ABANDONED`;
3. inspect worktree;
4. preserve any changes;
5. determine whether re-running Codex is safe;
6. increment a new attempt if required.

Partial files must not be silently committed.

---

# 91. Human Gate Recovery

Outstanding human gates survive restart unchanged.

After startup:

- existing gates remain valid;
- messages need not automatically be resent;
- overdue reminder policy may decide whether a reminder is appropriate.

---

# 92. Operational Invariants

The following rules must always hold:

1. privileged scheduling does not begin before startup recovery completes;
2. system health is tracked separately from project state;
3. waiting jobs do not consume scarce worker capacity;
4. one project cannot monopolise Codex workers indefinitely;
5. retry loops are bounded;
6. database backups occur before schema migration;
7. external actions are reconciled before replay;
8. merge actions are serialised initially;
9. low disk can prevent new Codex work;
10. completed projects remain queryable;
11. cancellation never implies repository deletion;
12. CI work is delegated externally where practical;
13. status queries remain available during most project failures;
14. configuration errors fail safely;
15. human silence never advances workflow.

---

# 93. Initial Operational Defaults

Suggested first-version defaults:

```text
Architect concurrency:             2
Codex concurrency:                 2
Repository provisioning:          1
Merge concurrency:                1

Codex timeout:                     60 minutes

Infrastructure retry attempts:     4
Codex workflow attempts:           5
CI rework cycles:                  5
Architect review cycles:           5

Database backup:                   daily
Backup before upgrade:             yes
Backup before migration:           yes

Disk warning:                      20% free
Stop new Codex work:               10% free
Critical disk alert:                5% free
```

All defaults must be configurable.

They should be tuned from real operational experience.

---

# 94. Deferred Operational Complexity

Version one does not require:

- Kubernetes;
- distributed scheduler;
- Redis queue;
- RabbitMQ;
- PostgreSQL;
- remote worker fleet;
- high-availability control plane;
- automatic failover host;
- full web administration console;
- enterprise on-call tooling.

These may be reconsidered if scale grows.

---

# 95. Future Operational Improvements

Potential future enhancements include:

- Telegram daily project summary;
- richer Grafana dashboards;
- remote Codex workers;
- automatic artifact pruning;
- webhook-driven GitHub updates;
- self-hosted GitHub runners;
- cost and token budgets;
- adaptive concurrency;
- project priority controls;
- scheduled maintenance mode;
- automatic restore testing;
- secondary standby Syntra host.

---

# 96. Initial Production Readiness Checklist

Before Syntra Build is considered production-capable:

- systemd service installed;
- service identities configured;
- credentials secured;
- SQLite WAL enabled;
- database backup configured;
- restart recovery tested;
- host reboot recovery tested;
- Codex timeout tested;
- Codex concurrency enforced;
- GitHub retry logic tested;
- PR idempotency tested;
- merge reconciliation tested;
- disk threshold behaviour tested;
- Prometheus metrics available;
- Grafana basic dashboard available;
- log rotation enabled;
- Telegram operator alerts working;
- blocked project isolation tested;
- multiple concurrent projects tested.

---

# 97. Operations Completion Criteria

The operations design is sufficiently complete when:

- the service lifecycle is defined;
- startup and shutdown order are defined;
- concurrency and scheduling policy are defined;
- Raspberry Pi resource protection is defined;
- retry and retry-exhaustion behaviour are defined;
- monitoring and health are defined;
- backups and recovery are defined;
- upgrade and rollback behaviour are defined;
- external-provider outages are isolated;
- project-level failures do not destabilise the system;
- operators have a deterministic recovery path;
- the platform can run continuously without regular shell intervention.

With `OPERATIONS.md` complete, the core architectural design set is sufficient to record the principal architectural decisions as ADRs and then produce the implementation `SPEC.md` and `AGENTS.md`.
