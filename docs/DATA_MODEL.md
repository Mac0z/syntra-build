# Syntra Build — Data Model

## 1. Purpose

This document defines the persistent data model for Syntra Build.

It translates the concepts defined in:

- `VISION.md`
- `WORKFLOW.md`
- `STATE_MACHINE.md`
- `ARCHITECTURE.md`
- `INTERFACES.md`

into durable entities and relationships suitable for implementation.

The model is designed to support:

- authoritative project state;
- milestone progression;
- asynchronous jobs;
- human gates;
- Architect conversations and reviews;
- Codex runs;
- Git workspaces and commits;
- GitHub repositories and pull requests;
- CI state;
- workflow events;
- state transition history;
- restart recovery;
- project status queries;
- auditability;
- multi-project concurrency.

The initial implementation will use SQLite.

This document defines the logical schema and intended persistence semantics.

Exact SQL migration files will be defined during implementation.

---

# 2. Data Model Principles

## 2.1 Persist state before side effects

Any workflow transition that may lead to an external side effect must be persisted before the action is attempted.

Examples:

- before repository creation;
- before Codex execution;
- before PR creation;
- before merge;
- before human-gate notification.

This allows restart recovery to determine what Syntra intended to do.

---

## 2.2 Current state and history are both required

Syntra should store:

- the current authoritative state for fast reads;
- immutable transition and event history for audit and recovery.

Current-state tables must not be treated as a replacement for historical records.

---

## 2.3 Stable internal identifiers

All primary domain entities should have Syntra-owned stable IDs.

Examples:

```text
proj_...
ms_...
job_...
gate_...
evt_...
review_...
codex_...
```

External identifiers such as GitHub repository IDs and PR numbers are stored separately.

---

## 2.4 External names are not keys

Project name, repository name, branch name and messaging labels are human-readable identifiers.

They must not replace internal primary keys.

---

## 2.5 Relationships must be explicit

Where an entity belongs to a project or milestone, that relationship should be represented with a foreign key.

Important joins should not depend on parsing strings.

---

## 2.6 JSON is for extensibility, not core identity

SQLite JSON fields may be used for:

- provider metadata;
- structured AI output;
- optional detail;
- raw external references;
- future-compatible extension data.

Core workflow fields such as:

- project state;
- milestone state;
- PR number;
- commit SHA;
- gate status;
- retry counters

should be represented as explicit columns.

---

# 3. Database Choice

The initial database is SQLite.

Recommended configuration:

```text
journal_mode = WAL
foreign_keys = ON
synchronous = NORMAL
busy_timeout = configured
```

Reasons include:

- single Syntra Build service;
- modest transaction volume;
- low operational overhead;
- strong transactional semantics;
- simple backups;
- good Raspberry Pi suitability.

---

# 4. Main Entity Groups

The persistent model is divided into the following groups.

## Project Definition

- `projects`
- `project_documents`
- `project_decisions`
- `project_change_requests`

## Workflow

- `milestones`
- `state_transitions`
- `workflow_events`
- `jobs`
- `job_attempts`
- `human_gates`
- `human_gate_responses`

## Messaging

- `messages`
- `notifications`

## Architect

- `architect_sessions`
- `architect_requests`
- `architect_responses`
- `architect_reviews`
- `architect_review_findings`

## Codex

- `codex_runs`
- `codex_run_tests`

## Git and Workspace

- `git_repositories`
- `git_workspaces`
- `change_sets`
- `commits`

## GitHub and CI

- `github_repositories`
- `pull_requests`
- `ci_runs`
- `ci_checks`
- `merge_attempts`

## Operational

- `errors`
- `recovery_observations`
- `system_metadata`

---

# 5. High-Level Relationships

```text
projects
   │
   ├── project_documents
   ├── project_decisions
   ├── project_change_requests
   ├── milestones
   │      │
   │      ├── jobs
   │      ├── human_gates
   │      ├── architect_reviews
   │      ├── codex_runs
   │      ├── git_workspaces
   │      ├── change_sets
   │      ├── commits
   │      ├── pull_requests
   │      └── ci_runs
   │
   ├── messages
   ├── notifications
   ├── architect_sessions
   ├── state_transitions
   ├── workflow_events
   ├── errors
   └── recovery_observations
```

---

# 6. projects

## 6.1 Purpose

Stores the authoritative top-level project record.

## 6.2 Core fields

```text
id
name
canonical_name
description
state
resume_state
activity
owner_user_id
repository_visibility
created_at
updated_at
completed_at
cancelled_at
last_state_change_at
active_milestone_id
messaging_chat_id
messaging_thread_id
github_repository_id
current_spec_revision
current_agents_revision
```

## 6.3 Field details

### `id`

Internal Syntra project ID.

Example:

```text
proj_flowtrack_01J...
```

Primary key.

---

### `name`

Human-readable project name.

Example:

```text
FlowTrack
```

---

### `canonical_name`

Normalised name used for repository creation and uniqueness checks.

Example:

```text
flowtrack
```

Must be unique.

---

### `description`

Short current project summary.

---

### `state`

Current project state.

Allowed values are defined by `STATE_MACHINE.md`.

Examples:

```text
DESIGNING
BUILDING
WAITING_HUMAN
COMPLETE
```

---

### `resume_state`

State to restore after `PAUSED` or certain `BLOCKED` conditions.

Nullable.

---

### `activity`

Short current operational activity string.

Example:

```text
Waiting for macOS GitHub Actions job
```

This is deliberately separate from state.

---

### `owner_user_id`

Authorised user who owns the project.

The initial version may support a single human owner, but the field should still exist explicitly.

---

### `repository_visibility`

Allowed values:

```text
public
private
```

Default:

```text
public
```

Private must reflect an explicitly approved design choice.

---

### `active_milestone_id`

Foreign key to the currently active milestone.

Nullable when:

- designing;
- provisioning;
- complete;
- cancelled.

---

### `github_repository_id`

Foreign key to Syntra's `github_repositories` row.

Nullable until provisioning succeeds.

---

## 6.4 Constraints

- `canonical_name` unique;
- project state must be valid;
- `repository_visibility` must be `public` or `private`;
- completed projects must not have an active milestone;
- cancelled projects must not have active jobs.

---

# 7. project_documents

## 7.1 Purpose

Stores versioned project design documents.

Initial document types include:

```text
SPEC
AGENTS
```

Other design artifacts may be supported later.

## 7.2 Core fields

```text
id
project_id
document_type
revision
status
content
content_hash
created_at
created_by
approved_at
approved_by
supersedes_document_id
```

---

## 7.3 Document statuses

```text
DRAFT
APPROVED
SUPERSEDED
REJECTED
```

---

## 7.4 Revision model

Example:

```text
SPEC revision 1 → APPROVED
SPEC revision 2 → DRAFT
SPEC revision 2 → APPROVED
SPEC revision 1 → SUPERSEDED
```

Only one approved active revision per document type should exist at a time.

---

## 7.5 `content_hash`

A cryptographic hash of the document content.

This helps verify that:

- the approved document committed to GitHub matches the approved Syntra artifact;
- later changes can be detected.

---

# 8. project_decisions

## 8.1 Purpose

Stores important project design or implementation decisions.

Examples:

- public repository selected;
- Windows support required;
- SQLite selected;
- particular architecture option chosen.

## 8.2 Core fields

```text
id
project_id
milestone_id
decision_type
title
decision
rationale
source
created_at
created_by
superseded_by_decision_id
```

## 8.3 Decision sources

Examples:

```text
HUMAN
ARCHITECT
POLICY
SYSTEM
```

Only a human or deterministic policy may resolve a human-required decision.

---

# 9. project_change_requests

## 9.1 Purpose

Stores requirement or scope changes submitted after the initial design baseline.

## 9.2 Core fields

```text
id
project_id
requested_by
request_text
classification
status
architect_analysis_json
spec_update_required
human_reapproval_required
created_at
resolved_at
```

## 9.3 Status values

```text
PENDING_ANALYSIS
ANALYSED
APPROVED
REJECTED
APPLIED
CANCELLED
```

---

# 10. milestones

## 10.1 Purpose

Stores all milestones defined by the approved project specification.

## 10.2 Core fields

```text
id
project_id
sequence_number
code
title
objective
state
resume_state
activity
definition_json
dependencies_json
automated_acceptance_json
human_acceptance_json
started_at
completed_at
created_at
updated_at
active_pull_request_id
current_codex_attempt
ci_rework_cycles
architect_review_cycles
```

---

## 10.3 Example

```text
id: ms_...
code: M4
title: Diagnostic Capture Optimisation
state: CI_RUNNING
activity: Waiting for macOS job
```

---

## 10.4 Constraints

- `(project_id, code)` unique;
- `(project_id, sequence_number)` unique;
- only one milestone per project may be actively executing in version one;
- complete milestones must reference a verified merged PR;
- cancelled projects cannot activate new milestones.

---

# 11. milestone_dependencies

Although milestone dependencies could be stored in JSON, a normalised table is preferred if dependency logic is used frequently.

## 11.1 Fields

```text
milestone_id
depends_on_milestone_id
```

Composite primary key.

This allows efficient dependency validation.

---

# 12. state_transitions

## 12.1 Purpose

Stores immutable state transition history.

## 12.2 Core fields

```text
id
entity_type
entity_id
project_id
milestone_id
previous_state
new_state
reason
trigger_event_id
actor_type
actor_id
correlation_id
created_at
metadata_json
```

## 12.3 Entity types

```text
PROJECT
MILESTONE
JOB
HUMAN_GATE
```

---

## 12.4 Rules

Rows are append-only.

They must never be updated to rewrite history.

---

# 13. workflow_events

## 13.1 Purpose

Stores immutable internal workflow events.

## 13.2 Core fields

```text
id
event_type
correlation_id
project_id
milestone_id
job_id
gate_id
occurred_at
received_at
payload_json
processed_at
processing_status
```

## 13.3 Processing status

```text
PENDING
PROCESSED
REJECTED
FAILED
```

---

## 13.4 Event uniqueness

Where an event originates from an external system, an external deduplication key should be stored to prevent duplicate processing.

---

# 14. jobs

## 14.1 Purpose

Stores schedulable units of work.

## 14.2 Core fields

```text
id
project_id
milestone_id
job_type
state
priority
correlation_id
attempt_number
max_attempts
scheduled_at
started_at
completed_at
next_retry_at
timeout_seconds
worker_class
payload_json
result_json
last_error_id
created_at
updated_at
```

---

## 14.3 Job states

Defined in `STATE_MACHINE.md`:

```text
QUEUED
DISPATCHED
RUNNING
WAITING_EXTERNAL
SUCCEEDED
RETRY_WAIT
FAILED
CANCELLED
ABANDONED
```

---

## 14.4 Worker classes

Suggested values:

```text
ARCHITECT
CODEX
GIT
GITHUB
CI
MESSAGING
RECOVERY
INTERNAL
```

Scheduler concurrency limits may operate by worker class.

---

# 15. job_attempts

## 15.1 Purpose

Stores the execution history of each job attempt.

A job may be retried without losing previous attempt details.

## 15.2 Core fields

```text
id
job_id
attempt_number
state
started_at
completed_at
external_request_id
process_id
exit_code
result_json
error_id
logs_reference
```

---

## 15.3 Constraint

```text
(job_id, attempt_number)
```

must be unique.

---

# 16. human_gates

## 16.1 Purpose

Stores all explicit human intervention points.

## 16.2 Core fields

```text
id
project_id
milestone_id
gate_type
state
title
prompt
expected_response_type
options_json
architect_recommendation
resume_project_state
resume_milestone_state
created_at
notified_at
responded_at
resolved_at
created_by
correlation_id
artifact_reference
```

---

## 16.3 Gate state

Defined in `STATE_MACHINE.md`:

```text
PENDING
NOTIFIED
RESPONDED
VALIDATED
RESOLVED
EXPIRED
CANCELLED
```

---

## 16.4 Constraints

- a resolved gate cannot return to an active state;
- a project in `WAITING_HUMAN` must have at least one unresolved gate;
- human silence must never create an automatic resolved state.

---

# 17. human_gate_responses

## 17.1 Purpose

Stores one or more responses associated with a human gate.

Multiple responses may exist if clarification is required.

## 17.2 Core fields

```text
id
gate_id
message_id
response_code
response_text
selected_option
attachments_json
responded_by
responded_at
validated
validation_notes
```

---

# 18. messages

## 18.1 Purpose

Stores relevant inbound and outbound messaging records.

This supports:

- project conversation reconstruction;
- gate correlation;
- audit;
- duplicate delivery detection.

## 18.2 Core fields

```text
id
project_id
milestone_id
gate_id
direction
platform
external_message_id
chat_id
thread_id
sender_id
message_type
text
attachments_json
reply_to_message_id
received_at
sent_at
correlation_id
raw_metadata_json
```

---

## 18.3 Direction

```text
INBOUND
OUTBOUND
```

---

## 18.4 Retention

Not every platform payload needs to be stored indefinitely.

The durable record should retain enough information to reconstruct project context and audit important actions.

---

# 19. notifications

## 19.1 Purpose

Tracks outbound notifications independently from message content.

Useful for retry and delivery-state handling.

## 19.2 Core fields

```text
id
project_id
milestone_id
gate_id
message_id
notification_type
priority
state
attempt_count
last_attempt_at
delivered_at
last_error_id
```

## 19.3 Notification states

```text
QUEUED
SENDING
DELIVERED
RETRY_WAIT
FAILED
CANCELLED
```

---

# 20. architect_sessions

## 20.1 Purpose

Stores optional provider conversation/session identifiers.

These are convenience references, not authoritative project state.

## 20.2 Core fields

```text
id
project_id
purpose
provider
model
external_session_id
created_at
last_used_at
status
metadata_json
```

## 20.3 Purpose values

Examples:

```text
DESIGN
IMPLEMENTATION
REVIEW
CHANGE_ANALYSIS
```

Syntra must remain able to recreate context without this row.

---

# 21. architect_requests

## 21.1 Purpose

Stores each Architect invocation.

## 21.2 Core fields

```text
id
project_id
milestone_id
job_id
session_id
request_type
provider
model
reasoning_level
request_schema_version
request_payload_json
correlation_id
started_at
completed_at
external_request_id
status
error_id
```

---

# 22. architect_responses

## 22.1 Purpose

Stores the normalised and optionally raw Architect output.

## 22.2 Core fields

```text
id
architect_request_id
response_type
response_schema_version
normalised_payload_json
raw_response_reference
status
created_at
validation_status
validation_error
```

---

# 23. architect_reviews

## 23.1 Purpose

Stores formal PR reviews.

## 23.2 Core fields

```text
id
project_id
milestone_id
architect_request_id
pull_request_id
reviewed_sha
verdict
summary
created_at
superseded_at
```

## 23.3 Verdicts

```text
APPROVE
CHANGES_REQUIRED
HUMAN_TEST_REQUIRED
HUMAN_DECISION_REQUIRED
BLOCKED
```

---

## 23.4 Approval freshness

Only an `APPROVE` review whose `reviewed_sha` equals the current PR head SHA is valid for merge.

Any later commit makes the previous approval stale.

The row should remain for history but may be marked superseded.

---

# 24. architect_review_findings

## 24.1 Purpose

Stores individual findings from Architect review.

## 24.2 Core fields

```text
id
review_id
finding_code
severity
requirement_ref
description
recommended_action
status
resolved_by_review_id
created_at
```

## 24.3 Finding statuses

```text
OPEN
RESOLVED
SUPERSEDED
ACCEPTED
```

---

# 25. codex_runs

## 25.1 Purpose

Stores each Codex invocation.

## 25.2 Core fields

```text
id
project_id
milestone_id
job_id
worktree_id
task_type
attempt_number
task_payload_json
process_status
process_id
started_at
completed_at
timeout_seconds
exit_code
summary
known_issues_json
stdout_reference
stderr_reference
error_id
```

---

## 25.3 Process status

```text
RUNNING
SUCCEEDED
FAILED
TIMED_OUT
CANCELLED
ABANDONED
```

---

## 25.4 Important distinction

A successful Codex run does not imply successful milestone implementation.

Milestone progress depends on:

- change validation;
- CI;
- Architect review;
- human gates;
- merge.

---

# 26. codex_run_tests

## 26.1 Purpose

Stores test commands and reported outcomes from a Codex run.

## 26.2 Core fields

```text
id
codex_run_id
command
status
summary
duration_ms
output_reference
```

These are useful for diagnostics but are not a substitute for required GitHub CI.

---

# 27. git_repositories

## 27.1 Purpose

Stores local repository metadata managed by Syntra.

## 27.2 Core fields

```text
id
project_id
repository_path
remote_name
remote_url
default_branch
last_fetch_at
last_known_main_sha
created_at
updated_at
```

---

# 28. git_workspaces

## 28.1 Purpose

Stores milestone worktree lifecycle information.

## 28.2 Core fields

```text
id
project_id
milestone_id
git_repository_id
branch_name
worktree_path
base_branch
base_sha
current_head_sha
state
created_at
last_validated_at
removed_at
```

## 28.3 Workspace states

```text
ACTIVE
DIRTY
READY
ORPHANED
REMOVED
ERROR
```

---

## 28.4 Constraints

- `(project_id, branch_name)` unique while active;
- only Syntra creates/removes workspaces;
- worktree path must remain inside the configured project workspace root.

---

# 29. change_sets

## 29.1 Purpose

Stores the deterministic Git diff observed after Codex execution.

## 29.2 Core fields

```text
id
project_id
milestone_id
worktree_id
codex_run_id
base_sha
head_sha_before_commit
diff_hash
is_empty
changed_files_json
added_files_json
deleted_files_json
validation_status
validation_result_json
created_at
```

---

## 29.3 Validation status

```text
PENDING
PASSED
FAILED
```

A `CommitRequest` must refer to a `PASSED` change set and its exact `diff_hash`.

---

# 30. commits

## 30.1 Purpose

Stores commits created by Syntra.

## 30.2 Core fields

```text
id
project_id
milestone_id
worktree_id
change_set_id
commit_sha
parent_sha
branch_name
message
author_name
author_email
created_at
pushed_at
```

---

## 30.3 Constraints

`commit_sha` unique.

---

# 31. github_repositories

## 31.1 Purpose

Stores authoritative GitHub repository identity.

## 31.2 Core fields

```text
id
project_id
external_repository_id
owner
name
full_name
visibility
default_branch
web_url
created_at
last_reconciled_at
repository_state
```

---

## 31.3 Repository state

```text
ACTIVE
ARCHIVED
MISSING
ERROR
```

---

## 31.4 Constraints

- `external_repository_id` unique;
- `full_name` unique;
- visibility must match approved project visibility.

---

# 32. pull_requests

## 32.1 Purpose

Stores GitHub PR state for each milestone.

## 32.2 Core fields

```text
id
project_id
milestone_id
github_repository_id
external_pr_number
state
head_branch
base_branch
head_sha
web_url
title
created_at
updated_at
merged_at
merge_commit_sha
closed_at
last_reconciled_at
```

---

## 32.3 PR states

```text
OPEN
MERGED
CLOSED
```

---

## 32.4 Constraints

- `(github_repository_id, external_pr_number)` unique;
- one active implementation PR per milestone;
- merged milestone must reference its PR.

---

# 33. ci_runs

## 33.1 Purpose

Stores CI evaluation for a specific PR head SHA.

## 33.2 Core fields

```text
id
project_id
milestone_id
pull_request_id
head_sha
overall_status
failure_classification
external_workflow_run_id
started_at
completed_at
last_checked_at
summary_json
```

---

## 33.3 Overall status

```text
QUEUED
RUNNING
PASSED
FAILED
CANCELLED
UNKNOWN
```

---

## 33.4 Freshness rule

A CI run is valid only for its exact `head_sha`.

When a new commit is pushed:

- the previous CI result remains historical;
- it must not be considered valid for merge.

---

# 34. ci_checks

## 34.1 Purpose

Stores individual required check outcomes.

## 34.2 Core fields

```text
id
ci_run_id
name
external_check_id
status
conclusion
started_at
completed_at
details_url
failure_summary
```

---

## 34.3 Check status

```text
QUEUED
RUNNING
COMPLETED
```

## 34.4 Conclusion

```text
PASSED
FAILED
CANCELLED
SKIPPED
NEUTRAL
UNKNOWN
```

---

# 35. merge_attempts

## 35.1 Purpose

Stores every privileged merge attempt.

## 35.2 Core fields

```text
id
project_id
milestone_id
pull_request_id
expected_head_sha
gatekeeper_result_json
merge_strategy
status
requested_at
completed_at
merge_commit_sha
error_id
```

## 35.3 Status

```text
REQUESTED
MERGED
NOT_MERGED
CONFLICT
REJECTED
UNKNOWN
```

---

# 36. errors

## 36.1 Purpose

Stores normalised operational errors.

## 36.2 Core fields

```text
id
project_id
milestone_id
job_id
component
classification
code
message
retryable
occurred_at
resolved_at
stack_reference
external_reference
user_safe_summary
metadata_json
```

---

## 36.3 Secret handling

Error rows must never persist:

- API tokens;
- passwords;
- private keys;
- full secret-bearing environment variables.

---

# 37. recovery_observations

## 37.1 Purpose

Stores facts discovered during startup or manual reconciliation.

## 37.2 Core fields

```text
id
project_id
milestone_id
job_id
observation_type
observed_at
details_json
reconciliation_action
processed_at
```

Examples:

```text
PR_EXISTS
PR_MISSING
CI_PASSED
CI_RUNNING
PROCESS_MISSING
WORKTREE_DIRTY
BRANCH_EXISTS
MERGE_ALREADY_COMPLETED
```

---

# 38. system_metadata

## 38.1 Purpose

Stores small amounts of instance-wide metadata.

Examples:

- database schema version;
- state-machine version;
- installation ID;
- last successful recovery time;
- last graceful shutdown time.

## 38.2 Core fields

```text
key
value
updated_at
```

Primary key is `key`.

---

# 39. Entity Relationship Summary

```text
projects
  │
  ├─1:N─ project_documents
  ├─1:N─ project_decisions
  ├─1:N─ project_change_requests
  ├─1:N─ milestones
  │       │
  │       ├─1:N─ jobs
  │       ├─1:N─ human_gates
  │       ├─1:N─ architect_reviews
  │       ├─1:N─ codex_runs
  │       ├─1:N─ git_workspaces
  │       ├─1:N─ change_sets
  │       ├─1:N─ commits
  │       ├─1:1/0─ active pull_request
  │       └─1:N─ ci_runs
  │
  ├─1:N─ messages
  ├─1:N─ architect_requests
  ├─1:N─ state_transitions
  ├─1:N─ workflow_events
  ├─1:N─ errors
  └─1:N─ recovery_observations
```

---

# 40. Current-State versus Historical Tables

## Current-state focused

The following tables primarily expose the current operational state:

```text
projects
milestones
jobs
human_gates
git_workspaces
github_repositories
pull_requests
```

These should be optimised for frequent reads.

---

## Historical / audit focused

The following tables primarily preserve history:

```text
state_transitions
workflow_events
job_attempts
human_gate_responses
architect_requests
architect_responses
architect_reviews
architect_review_findings
codex_runs
change_sets
commits
ci_runs
ci_checks
merge_attempts
errors
recovery_observations
```

---

# 41. Transaction Boundaries

Important state changes should occur inside explicit database transactions.

Examples:

## Activate milestone

Transaction:

1. verify project state;
2. verify no other active milestone;
3. set milestone `READY → PREPARING_TASK`;
4. set project `READY → BUILDING`;
5. set `active_milestone_id`;
6. append state transitions;
7. append workflow event;
8. create Architect job;
9. commit.

Only after commit may the Architect request begin.

---

## Create human gate

Transaction:

1. create `human_gates` row;
2. set milestone to human-gate state;
3. set project to `WAITING_HUMAN`;
4. append transition records;
5. create notification job;
6. commit.

Only then should Telegram notification be sent.

---

## Prepare merge

Transaction:

1. verify merge guards;
2. create merge attempt record;
3. set milestone `MERGE_READY → MERGING`;
4. append state transition;
5. create merge job;
6. commit.

Only then should the GitHub merge call be executed.

---

# 42. Optimistic Concurrency

Although Syntra initially runs as a single service, asynchronous tasks may race.

Important mutable entities should therefore include a version field.

Recommended:

```text
row_version INTEGER NOT NULL DEFAULT 1
```

for:

- projects;
- milestones;
- jobs;
- human_gates;
- pull_requests.

Updates should use:

```text
WHERE id = ? AND row_version = ?
```

and increment the version.

This helps prevent accidental stale writes.

---

# 43. Time Representation

All persisted timestamps should use UTC.

Application-level representation should be timezone-aware.

Human-facing timestamps may be converted to the user's local timezone by the messaging layer.

Recommended stored form:

```text
2026-09-06T20:30:00.000Z
```

or an equivalent consistent UTC format.

---

# 44. Identifier Strategy

Syntra-owned IDs should be:

- globally unique;
- sortable where practical;
- safe to log;
- opaque to users.

ULID-style identifiers are a good fit.

Examples:

```text
proj_01J...
ms_01J...
job_01J...
gate_01J...
evt_01J...
```

External IDs remain in separate fields.

---

# 45. Uniqueness Constraints

Important uniqueness rules include:

```text
projects.canonical_name

project_documents:
(project_id, document_type, revision)

milestones:
(project_id, code)
(project_id, sequence_number)

job_attempts:
(job_id, attempt_number)

git_workspaces:
(project_id, branch_name) while active

commits.commit_sha

github_repositories.external_repository_id
github_repositories.full_name

pull_requests:
(github_repository_id, external_pr_number)

ci_runs:
(pull_request_id, head_sha, external_workflow_run_id) where appropriate
```

---

# 46. Referential Integrity

SQLite foreign keys should be enabled.

Deletion policy should favour preservation.

In most cases:

```text
ON DELETE RESTRICT
```

or:

```text
ON DELETE SET NULL
```

is preferable to cascade deletion.

Syntra Build is an audit-oriented workflow system.

Deleting a project should not erase its execution history.

Project cancellation is represented by state, not row deletion.

---

# 47. Soft Deletion

Core workflow entities should not be physically deleted during normal operation.

Where cleanup is needed, use fields such as:

```text
archived_at
removed_at
cancelled_at
```

Examples:

- removed worktree;
- archived project;
- obsolete session.

---

# 48. Indexing Strategy

Useful initial indexes include:

```text
projects(state)
projects(active_milestone_id)

milestones(project_id, state)
milestones(project_id, sequence_number)

jobs(state, scheduled_at)
jobs(project_id, state)
jobs(worker_class, state)

human_gates(project_id, state)
human_gates(state)

workflow_events(processing_status, occurred_at)
workflow_events(project_id, occurred_at)

state_transitions(project_id, created_at)
state_transitions(milestone_id, created_at)

messages(project_id, received_at)
messages(chat_id, thread_id, received_at)

architect_requests(project_id, created_at)
codex_runs(project_id, milestone_id, started_at)

pull_requests(project_id, state)
ci_runs(pull_request_id, head_sha)

errors(project_id, occurred_at)
```

Indexes should be reviewed against actual query patterns once the system is running.

---

# 49. Status Query Projection

The Status Service should be able to construct a project summary efficiently from current-state tables.

Typical read set:

```text
projects
    ↓
active milestone
    ↓
active PR
    ↓
latest CI run
    ↓
latest Architect review
    ↓
unresolved human gates
    ↓
latest error
```

This should not require replaying event history.

---

# 50. Example Status Projection

Given:

```text
projects.state = BUILDING
projects.activity = "Waiting for GitHub Actions"

milestones.code = M4
milestones.state = CI_RUNNING

pull_requests.external_pr_number = 12
pull_requests.head_sha = abc123

ci_runs.overall_status = RUNNING

ci_checks:
windows = PASSED
macos   = RUNNING
```

Syntra can produce:

> FlowTrack is currently on M4 — Diagnostic Capture Optimisation. PR #12 is in GitHub Actions. Windows has passed and macOS is still running. No action is required from you. The next step is Architect review if CI passes.

---

# 51. Human Action Query

The query:

> Is anything waiting for me?

should primarily read:

```text
human_gates
WHERE state IN ('PENDING', 'NOTIFIED', 'RESPONDED')
```

joined to:

```text
projects
milestones
```

This should be a fast, deterministic query.

---

# 52. Active Projects Query

The query:

> What projects are active?

should use project states excluding:

```text
COMPLETE
CANCELLED
FAILED
```

unless explicitly requested.

Suggested summary fields:

- project name;
- current project state;
- milestone code/title;
- milestone state;
- activity;
- human action required.

---

# 53. Recovery Queries

Startup recovery should be able to efficiently identify:

## Active projects

```text
projects
WHERE state NOT IN ('COMPLETE', 'CANCELLED')
```

## Unfinished jobs

```text
jobs
WHERE state IN (
  'DISPATCHED',
  'RUNNING',
  'WAITING_EXTERNAL',
  'RETRY_WAIT'
)
```

## Active workspaces

```text
git_workspaces
WHERE state IN ('ACTIVE', 'DIRTY', 'READY')
```

## Open PRs

```text
pull_requests
WHERE state = 'OPEN'
```

## Unresolved gates

```text
human_gates
WHERE state NOT IN ('RESOLVED', 'CANCELLED')
```

---

# 54. Retry Counters

Retry counters exist in two places.

## Job-level

```text
jobs.attempt_number
jobs.max_attempts
job_attempts
```

Used for infrastructure-level retry.

---

## Milestone-level

```text
milestones.current_codex_attempt
milestones.ci_rework_cycles
milestones.architect_review_cycles
```

Used to detect workflow loops.

These serve different purposes and should not be conflated.

---

# 55. Document and Repository Consistency

After repository provisioning, Syntra should verify:

```text
project_documents.content_hash
```

against the actual committed content of:

```text
SPEC.md
AGENTS.md
```

The repository baseline should record:

- corresponding document revision;
- commit SHA;
- content hash.

This linkage may be stored in `project_documents` metadata or a dedicated repository-document mapping if implementation needs it.

---

# 56. SHA-Based Freshness Model

Three important entities depend on exact commit identity:

```text
CI result
Architect review
Human-tested artifact
```

All three must ultimately be relatable to the current PR head SHA.

For merge eligibility:

```text
current PR head SHA
    =
latest passing CI head SHA
    =
latest Architect-approved reviewed SHA
```

and, where human testing is required:

```text
human-tested artifact must correspond to the same approved code revision
```

The database must preserve these associations explicitly.

---

# 57. Human Test Artifact Linkage

Where human testing uses a generated artifact, the human gate should store:

```text
artifact_reference
tested_head_sha
ci_run_id
```

or equivalent metadata.

This prevents a test result for an older build from accidentally approving a newer PR head.

---

# 58. Raw Provider Data

Raw provider payloads may be useful for debugging.

They should not be stored inline in every operational row if they are large.

Preferred approach:

- persist normalised data in SQLite;
- store large raw payloads/logs as files or compressed artifacts;
- store a reference in the database.

Examples:

```text
raw_response_reference
stdout_reference
stderr_reference
output_reference
```

---

# 59. Large Text Fields

Potentially large values include:

- `SPEC.md`;
- `AGENTS.md`;
- Architect prompts/responses;
- Codex logs;
- Git diffs;
- CI log extracts.

SQLite can store text, but very large raw artifacts should be externalised where appropriate.

The database should store:

- concise normalised data;
- hashes;
- references.

---

# 60. Artifact Storage

A likely local artifact structure is:

```text
/var/lib/syntra-build/artifacts/
    <project_id>/
        architect/
        codex/
        ci/
        human-tests/
        logs/
```

Artifact paths should be generated by Syntra.

External input must never directly control filesystem paths.

---

# 61. Retention Policy

The initial system should preserve:

- project history;
- milestone history;
- human decisions;
- Architect reviews;
- commits;
- PR and CI metadata;
- state transitions;
- workflow events;
- errors.

Potentially high-volume data such as:

- raw API payloads;
- verbose Codex stdout;
- CI log excerpts;
- duplicate messaging metadata

may later have configurable retention periods.

---

# 62. Backup Scope

The following represent unique Syntra state and must be backed up:

```text
syntra-build.db
configuration
project documents not yet committed
project decision history
human gates
local unpushed worktree changes
local artifacts not reproducible externally
```

Data already safely stored in GitHub can generally be reconstructed.

---

# 63. Database Migrations

The application must use explicit schema migrations.

Each migration should have:

```text
version
description
applied_at
checksum
```

Migration execution should occur before normal workflow recovery.

If migration fails:

- Syntra must not resume privileged workflow actions;
- startup should fail safely;
- project data must not be partially migrated.

---

# 64. State-Machine Versioning

The database should store the state-machine version used by persisted workflow state.

Suggested:

```text
system_metadata:
state_machine_version = 1
```

If future releases materially change state semantics, migration logic may need to translate existing active projects.

---

# 65. Database Integrity Checks

On startup, Syntra should perform lightweight integrity checks such as:

- foreign keys enabled;
- schema version supported;
- required tables present;
- no duplicate active milestones per project;
- no multiple active PRs for one milestone;
- waiting-human projects have unresolved gates;
- current referenced entities exist.

Serious integrity failures should block automatic workflow execution.

---

# 66. Audit Query Examples

The model should make questions like these easy to answer:

> Why was PR #12 not merged?

> How many Codex attempts did M4 require?

> Which Architect finding caused rework?

> When did the user approve the design?

> Which CI run was approved?

> What happened before the Syntra restart?

> Why is the project blocked?

This is a major reason to preserve structured history rather than only current state.

---

# 67. Example Milestone Data Chain

A single milestone may generate the following data chain:

```text
milestone M4
    ↓
ArchitectTaskRequest / response
    ↓
job: CODEX_RUN
    ↓
codex_run attempt 1
    ↓
change_set 1
    ↓
commit abc123
    ↓
pull_request #12
    ↓
ci_run for abc123
    ↓
architect_review:
CHANGES_REQUIRED
    ↓
finding 1
    ↓
codex_run attempt 2
    ↓
change_set 2
    ↓
commit def456
    ↓
same pull_request #12
new head = def456
    ↓
new ci_run
    ↓
architect_review:
APPROVE
    ↓
merge_attempt
    ↓
merge verified
    ↓
milestone COMPLETE
```

All earlier attempts remain available historically.

---

# 68. Example Project Design Data Chain

```text
project created
    ↓
messages
    ↓
architect design requests/responses
    ↓
project decisions
    ↓
SPEC draft revision 1
AGENTS draft revision 1
    ↓
human design gate
    ↓
REQUEST_CHANGES
    ↓
SPEC draft revision 2
AGENTS draft revision 2
    ↓
human design gate
    ↓
APPROVE
    ↓
documents revision 2 = APPROVED
    ↓
repository provisioning
```

The complete design history remains reconstructable.

---

# 69. Multi-Project Isolation

Every project-scoped entity must carry `project_id`.

Milestone entities additionally carry `milestone_id`.

The scheduler and status queries must always filter by project ownership.

No workspace, PR, gate, or Codex result from one project should ever be resolvable solely by a globally ambiguous human-readable name.

---

# 70. Data Model Invariants

The following invariants must always hold.

## Project

1. `canonical_name` is unique.
2. Public is the default repository visibility.
3. Private visibility must originate from approved design data.
4. A completed project has no active milestone.
5. A cancelled project has no active execution jobs.

## Milestone

6. One active implementation milestone per project in version one.
7. One active implementation PR per milestone.
8. A complete milestone has a verified merged PR.
9. `CI_RUNNING` requires an open PR.
10. `ARCHITECT_REVIEW` requires passing CI for the current SHA.

## SHA freshness

11. Merge requires CI pass for current PR head SHA.
12. Merge requires Architect approval for current PR head SHA.
13. Required human testing must correspond to the relevant code revision.

## Human gates

14. A waiting-human project has at least one unresolved gate.
15. A resolved gate cannot be responded to again.
16. No response is ever inferred from silence.

## Jobs

17. Job attempt numbers are monotonically increasing.
18. Terminal job attempts are immutable.
19. Running jobs belong to valid, non-terminal projects.

## Audit

20. State transitions are append-only.
21. Workflow events are append-only.
22. Existing historical rows are not rewritten to hide earlier outcomes.

---

# 71. Initial Query Performance Targets

The database should easily support:

- dozens of projects;
- hundreds of milestones;
- thousands of jobs;
- thousands to tens of thousands of events;
- years of audit history

on the Syntra Raspberry Pi without requiring a separate database server.

Status queries should normally complete using indexed current-state tables rather than event replay.

---

# 72. Deferred Data Complexity

The initial data model deliberately does not include:

- multi-user role tables;
- billing;
- AI cost accounting;
- generic plugin registry;
- generic artifact graph;
- distributed worker leases;
- multi-host scheduler state;
- full-text search infrastructure;
- analytics warehouse.

These may be introduced later if required.

---

# 73. Future Extension Points

Possible future additions include:

- `users`;
- `roles`;
- `project_members`;
- `deployments`;
- `releases`;
- `preview_environments`;
- `artifacts`;
- `cost_usage`;
- `provider_accounts`;
- `worker_hosts`;
- `scheduled_tasks`.

The core foreign-key model should make these straightforward to add.

---

# 74. Suggested Initial Schema Order

Migration order will likely be:

```text
1. system_metadata
2. projects
3. project_documents
4. project_decisions
5. project_change_requests
6. milestones
7. milestone_dependencies
8. jobs
9. job_attempts
10. human_gates
11. human_gate_responses
12. messages
13. notifications
14. architect_sessions
15. architect_requests
16. architect_responses
17. github_repositories
18. git_repositories
19. git_workspaces
20. codex_runs
21. codex_run_tests
22. change_sets
23. commits
24. pull_requests
25. ci_runs
26. ci_checks
27. architect_reviews
28. architect_review_findings
29. merge_attempts
30. workflow_events
31. state_transitions
32. errors
33. recovery_observations
```

Exact ordering may change to satisfy foreign-key dependencies.

---

# 75. Data Model Completion Criteria

The data model is sufficiently complete when:

- every stateful domain concept has a persistent representation;
- current project status can be read efficiently;
- every workflow transition can be audited;
- every external side effect can be reconciled;
- Architect and Codex runs can be traced to project and milestone;
- every PR, CI result, review, and merge is tied to exact commit identity;
- human gates and responses are durable and correlated;
- restart recovery can reconstruct all active work;
- multi-project execution remains isolated;
- the model can be implemented cleanly in SQLite without distributed infrastructure.

The next design document should be `SECURITY.md`, defining the trust model, credentials, sandboxing, permissions, secrets handling, and privileged-operation controls required to protect this architecture.
