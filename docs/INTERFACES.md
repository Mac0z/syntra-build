# Syntra Build — Interfaces

## 1. Purpose

This document defines the logical contracts used by Syntra Build components and external integrations.

It specifies:

- which component owns each interface;
- which component consumes each interface;
- required fields;
- validation rules;
- correlation and idempotency requirements;
- permitted outcomes;
- the workflow events each interface may generate.

The goal is to ensure that the orchestration engine does not depend on loosely interpreted prose or provider-specific behaviour.

Where AI components are involved, structured data should be used whenever the output can affect workflow state.

This document builds on:

- `VISION.md`
- `WORKFLOW.md`
- `STATE_MACHINE.md`
- `ARCHITECTURE.md`

---

# 2. Interface Principles

## 2.1 Stable domain contracts

Core orchestration code should exchange stable Syntra domain objects rather than provider-specific payloads.

For example:

```text
ArchitectReview
```

is a Syntra contract.

It should not expose raw OpenAI response structure to the rest of the application.

---

## 2.2 Provider adapters translate

Adapters are responsible for converting:

```text
Provider-specific input/output
        ↕
Syntra domain contract
```

The domain layer should not need to understand:

- Telegram update JSON;
- OpenAI response envelopes;
- Codex CLI output format;
- GitHub API response bodies.

---

## 2.3 Workflow-changing outputs must be structured

Any external or AI response that may drive a state transition must be converted into a structured, validated object before the transition is considered.

Examples include:

- Architect verdicts;
- Codex completion summaries;
- CI results;
- human gate responses;
- GitHub PR state;
- merge results.

---

## 2.4 External identifiers are explicit

Interfaces should include explicit external identifiers where relevant.

Examples:

- GitHub repository ID;
- GitHub PR number;
- commit SHA;
- Telegram chat ID;
- Telegram thread ID;
- provider request ID.

Names alone should not be used where a stable external identifier exists.

---

## 2.5 Correlation is mandatory

All workflow-changing operations should carry a `correlation_id`.

Where appropriate they should also carry:

- `project_id`;
- `milestone_id`;
- `job_id`;
- `gate_id`;
- `attempt_number`.

This allows Syntra to determine exactly which workflow instance a result belongs to.

---

## 2.6 Idempotency is required for side effects

Operations that can create or mutate external state should use an idempotency strategy where practical.

Examples:

- repository creation;
- PR creation;
- human-gate creation;
- merge;
- notification send;
- retryable external jobs.

The implementation may use:

- explicit idempotency keys;
- external-state reconciliation;
- unique branch naming;
- unique workflow identifiers;
- lookup-before-create logic.

---

# 3. Interface Categories

The initial interface set is grouped as follows:

## Human and Messaging

- `InboundMessage`
- `OutboundMessage`
- `ProjectCommand`
- `HumanGateRequest`
- `HumanGateResponse`
- `ProjectStatusRequest`
- `ProjectStatusResponse`

## Architect

- `ArchitectDesignRequest`
- `ArchitectDesignResponse`
- `SpecificationDraft`
- `ArchitectTaskRequest`
- `ArchitectTask`
- `ArchitectReviewRequest`
- `ArchitectReview`
- `ArchitectChangeAnalysisRequest`
- `ArchitectChangeAnalysis`

## Codex

- `CodexRunRequest`
- `CodexRunResult`

## Git and Workspace

- `WorkspaceRequest`
- `WorkspaceDescriptor`
- `ChangeSet`
- `CommitRequest`
- `CommitResult`
- `PushResult`

## GitHub and CI

- `RepositoryProvisionRequest`
- `RepositoryDescriptor`
- `PullRequestCreateRequest`
- `PullRequestDescriptor`
- `CIStatus`
- `CIResult`
- `MergeRequest`
- `MergeResult`

## Workflow and Operations

- `WorkflowEvent`
- `JobDescriptor`
- `ErrorRecord`
- `RecoveryObservation`

---

# 4. Common Metadata

Most domain contracts should use a common metadata envelope.

Example:

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_...",
  "milestone_id": "ms_...",
  "job_id": "job_...",
  "created_at": "2026-09-06T21:00:00Z"
}
```

Not every field is mandatory for every interface.

The general rules are:

- `interface_version` is required;
- `correlation_id` is required for workflow-changing operations;
- `project_id` is required for project-scoped operations;
- `milestone_id` is required for milestone-scoped operations;
- `job_id` is required for execution jobs;
- timestamps should use UTC ISO 8601.

---

# 5. InboundMessage

## 5.1 Purpose

Represents a message received from the human-facing messaging platform.

## 5.2 Producer

Messaging Gateway.

## 5.3 Consumer

Command and Intent Router.

## 5.4 Required fields

```json
{
  "interface_version": "1.0",
  "message_id": "msg_...",
  "platform": "telegram",
  "external_message_id": "12345",
  "chat_id": "67890",
  "thread_id": "111",
  "sender_id": "user_...",
  "received_at": "2026-09-06T21:00:00Z",
  "text": "What is happening with FlowTrack?"
}
```

## 5.5 Optional fields

- attachments;
- reply-to message ID;
- project hint;
- gate hint;
- raw platform metadata reference.

## 5.6 Validation rules

- sender must be authorised;
- message ID must be unique;
- duplicate platform delivery must not create duplicate workflow actions;
- attachments must be validated separately;
- empty messages are ignored unless they contain supported attachments.

---

# 6. OutboundMessage

## 6.1 Purpose

Represents a message Syntra wants to send to the human.

## 6.2 Producer

Status Service, Human Gate Manager, Orchestration Engine, Notification Service.

## 6.3 Consumer

Messaging Gateway.

## 6.4 Required fields

```json
{
  "interface_version": "1.0",
  "message_id": "out_...",
  "correlation_id": "01J...",
  "chat_id": "67890",
  "text": "FlowTrack M4 is waiting for GitHub Actions.",
  "priority": "normal"
}
```

## 6.5 Optional fields

- thread ID;
- project ID;
- milestone ID;
- gate ID;
- buttons/actions;
- attachment references;
- retry policy.

## 6.6 Priority values

```text
low
normal
high
critical
```

Routine progress messages should normally be `normal`.

Human-action requests should normally be `high`.

---

# 7. ProjectCommand

## 7.1 Purpose

Represents a human instruction that may affect project workflow.

## 7.2 Producer

Command and Intent Router.

## 7.3 Consumer

Orchestration Engine.

## 7.4 Command types

```text
CREATE_PROJECT
PAUSE_PROJECT
RESUME_PROJECT
CANCEL_PROJECT
REQUEST_STATUS
REQUEST_FULL_STATUS
REQUEST_ACTIVE_PROJECTS
REQUEST_HUMAN_ACTIONS
SUBMIT_PROJECT_CHANGE
```

## 7.5 Example

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "command": "PAUSE_PROJECT",
  "project_id": "proj_flowtrack",
  "requested_by": "user_...",
  "requested_at": "2026-09-06T21:00:00Z"
}
```

## 7.6 Validation rules

State-changing commands must be validated against:

- authorised user;
- current project state;
- allowed state transition;
- unresolved privileged actions;
- gatekeeper rules where applicable.

---

# 8. HumanGateRequest

## 8.1 Purpose

Represents an explicit request for human action.

## 8.2 Producer

Human Gate Manager.

## 8.3 Consumer

Messaging Gateway and Persistence Layer.

## 8.4 Required fields

```json
{
  "interface_version": "1.0",
  "gate_id": "gate_...",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "gate_type": "HUMAN_TEST",
  "title": "Test FlowTrack M4 on macOS",
  "prompt": "Please verify that diagnostic capture remains enabled after restart.",
  "expected_response_type": "PASS_FAIL_WITH_NOTES",
  "resume_target": "MERGE_READY",
  "created_at": "2026-09-06T21:00:00Z"
}
```

## 8.5 Gate types

```text
DESIGN_APPROVAL
PRODUCT_DECISION
TECHNICAL_DECISION
HUMAN_TEST
FINAL_ACCEPTANCE
RECOVERY_DECISION
```

## 8.6 Optional fields

- option list;
- Architect recommendation;
- test steps;
- artifact reference;
- external build URL;
- expiry policy;
- severity.

---

# 9. HumanGateResponse

## 9.1 Purpose

Represents a human response correlated to an open gate.

## 9.2 Producer

Command and Intent Router.

## 9.3 Consumer

Human Gate Manager.

## 9.4 Required fields

```json
{
  "interface_version": "1.0",
  "gate_id": "gate_...",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "response": "PASS",
  "responded_by": "user_...",
  "responded_at": "2026-09-06T21:15:00Z"
}
```

## 9.5 Optional fields

- free-text notes;
- attachments;
- screenshots;
- logs;
- selected option.

## 9.6 Validation rules

- referenced gate must exist;
- gate must not already be resolved;
- project must match;
- sender must be authorised;
- response must match expected response type.

---

# 10. ProjectStatusRequest

## 10.1 Purpose

Represents a read-only request for project status.

## 10.2 Producer

Command and Intent Router.

## 10.3 Consumer

Status Service.

## 10.4 Request types

```text
SINGLE_PROJECT
FULL_PROJECT
ACTIVE_PROJECTS
HUMAN_ACTIONS
SYSTEM_SUMMARY
```

## 10.5 Example

```json
{
  "interface_version": "1.0",
  "request_id": "status_...",
  "request_type": "SINGLE_PROJECT",
  "project_id": "proj_flowtrack",
  "requested_by": "user_..."
}
```

Status requests must never mutate project workflow.

---

# 11. ProjectStatusResponse

## 11.1 Purpose

Represents authoritative project status.

## 11.2 Producer

Status Service.

## 11.3 Consumer

Messaging Gateway.

## 11.4 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "project_name": "FlowTrack",
  "project_state": "BUILDING",
  "milestone_id": "ms_m4",
  "milestone_name": "Diagnostic Capture Optimisation",
  "milestone_state": "CI_RUNNING",
  "activity": "Waiting for macOS package job",
  "human_action_required": false,
  "next_action": "Architect review after CI passes",
  "state_updated_at": "2026-09-06T21:10:00Z"
}
```

## 11.5 Optional fields

- PR number;
- PR URL;
- branch;
- CI summary;
- Codex attempt;
- latest Architect verdict;
- retry count;
- last error;
- outstanding gate;
- milestone progress;
- project progress.

---

# 12. ArchitectDesignRequest

## 12.1 Purpose

Requests design reasoning or continuation of a project design conversation.

## 12.2 Producer

Orchestration Engine.

## 12.3 Consumer

Architect Adapter.

## 12.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "project_name": "FlowTrack",
  "initial_request": "Build a desktop application...",
  "conversation_context": [],
  "known_decisions": [],
  "open_questions": []
}
```

## 12.5 Optional fields

- previous design summary;
- global architecture policies;
- repository visibility preference;
- uploaded references;
- project constraints.

---

# 13. ArchitectDesignResponse

## 13.1 Purpose

Returns the Architect's next design contribution.

## 13.2 Producer

Architect Adapter.

## 13.3 Consumer

Orchestration Engine.

## 13.4 Response modes

```text
ASK_USER
PROPOSE_DESIGN
READY_TO_DRAFT
BLOCKED
```

## 13.5 Example

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "mode": "ASK_USER",
  "message": "Should the application support Windows as well as macOS?",
  "new_decisions": [],
  "open_questions": [
    "target_platforms"
  ]
}
```

The free-text message may be conversational, but `mode` must be structured.

---

# 14. SpecificationDraft

## 14.1 Purpose

Represents the formal design package produced before human approval.

## 14.2 Producer

Architect Adapter.

## 14.3 Consumer

Orchestration Engine and Human Gate Manager.

## 14.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "spec_markdown": "...",
  "agents_markdown": "...",
  "design_summary": "...",
  "repository_visibility": "public",
  "milestone_count": 8,
  "assumptions": [],
  "open_non_blocking_items": []
}
```

## 14.5 Repository visibility values

```text
public
private
```

Default is `public`.

`private` must be supported by explicit approved project intent.

---

# 15. ArchitectTaskRequest

## 15.1 Purpose

Requests a milestone implementation task from the Architect.

## 15.2 Producer

Orchestration Engine.

## 15.3 Consumer

Architect Adapter.

## 15.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "spec_revision": "spec_3",
  "agents_revision": "agents_2",
  "milestone_definition": {},
  "repository_context": {},
  "previous_milestone_summaries": []
}
```

---

# 16. ArchitectTask

## 16.1 Purpose

Defines exactly what Codex should implement for a milestone or rework cycle.

## 16.2 Producer

Architect Adapter.

## 16.3 Consumer

Codex Runner.

## 16.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "task_type": "IMPLEMENT",
  "objective": "Implement diagnostic capture optimisation.",
  "requirements": [],
  "acceptance_criteria": [],
  "constraints": [],
  "tests_required": [],
  "explicit_exclusions": []
}
```

## 16.5 Task types

```text
IMPLEMENT
CI_REWORK
REVIEW_REWORK
HUMAN_TEST_REWORK
```

## 16.6 Optional fields

- files of interest;
- architecture notes;
- prior failure summary;
- prior review findings;
- human feedback;
- recommended commands.

## 16.7 Validation rules

The task must:

- reference the current project;
- reference the current milestone;
- use the current specification revision;
- avoid privileged GitHub instructions;
- remain within milestone scope unless a formally approved scope change exists.

---

# 17. ArchitectReviewRequest

## 17.1 Purpose

Requests architectural and implementation review of the current PR head.

## 17.2 Producer

Orchestration Engine.

## 17.3 Consumer

Architect Adapter.

## 17.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "pull_request_number": 12,
  "head_sha": "abc123",
  "milestone_definition": {},
  "specification": "...",
  "agents_instructions": "...",
  "diff": "...",
  "ci_result": {},
  "previous_findings": []
}
```

The `head_sha` is critical.

Architect approval is valid only for the exact reviewed SHA.

---

# 18. ArchitectReview

## 18.1 Purpose

Represents the Architect's formal verdict on a PR head.

## 18.2 Producer

Architect Adapter.

## 18.3 Consumer

Orchestration Engine.

## 18.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "pull_request_number": 12,
  "reviewed_sha": "abc123",
  "verdict": "CHANGES_REQUIRED",
  "summary": "The implementation is close but two acceptance criteria are incomplete.",
  "findings": []
}
```

## 18.5 Verdict values

```text
APPROVE
CHANGES_REQUIRED
HUMAN_TEST_REQUIRED
HUMAN_DECISION_REQUIRED
BLOCKED
```

## 18.6 Finding structure

```json
{
  "finding_id": "finding_1",
  "severity": "major",
  "requirement_ref": "M4.3",
  "description": "The setting is not persisted between launches.",
  "recommended_action": "Persist the value using the existing preferences service."
}
```

## 18.7 Severity values

```text
info
minor
major
critical
```

## 18.8 Validation rules

An Architect review must be rejected if:

- project mismatch;
- milestone mismatch;
- PR mismatch;
- reviewed SHA does not match requested SHA;
- verdict is not recognised;
- required findings are missing for `CHANGES_REQUIRED`;
- required human request detail is missing for human-gate verdicts.

---

# 19. ArchitectChangeAnalysisRequest

## 19.1 Purpose

Requests impact analysis for a human-requested requirement change during build.

## 19.2 Producer

Orchestration Engine.

## 19.3 Consumer

Architect Adapter.

## 19.4 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "change_request": "Also compress diagnostic files automatically.",
  "current_specification": "...",
  "completed_milestones": [],
  "active_milestone": {}
}
```

---

# 20. ArchitectChangeAnalysis

## 20.1 Purpose

Describes the impact of a proposed scope or requirement change.

## 20.2 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "classification": "MATERIAL",
  "affected_requirements": [],
  "affected_milestones": [],
  "completed_work_affected": false,
  "spec_update_required": true,
  "human_reapproval_required": true,
  "recommendation": "Add a new milestone after M4."
}
```

## 20.3 Classification values

```text
MINOR
MATERIAL
ARCHITECTURAL
OUT_OF_SCOPE
```

---

# 21. CodexRunRequest

## 21.1 Purpose

Represents one controlled coding-agent execution.

## 21.2 Producer

Orchestration Engine.

## 21.3 Consumer

Codex Runner.

## 21.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "job_id": "job_codex_4_2",
  "attempt_number": 2,
  "worktree_path": "/var/lib/syntra-build/projects/flowtrack/worktrees/m04",
  "task": {},
  "agents_markdown": "...",
  "timeout_seconds": 3600
}
```

## 21.5 Optional fields

- allowed commands;
- environment overrides;
- test command hints;
- previous run summary.

## 21.6 Security rules

The Codex environment must exclude privileged GitHub credentials and Syntra secrets.

---

# 22. CodexRunResult

## 22.1 Purpose

Represents Codex process completion.

## 22.2 Producer

Codex Runner.

## 22.3 Consumer

Orchestration Engine.

## 22.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "job_id": "job_codex_4_2",
  "attempt_number": 2,
  "process_status": "SUCCEEDED",
  "summary": "Implemented persistent diagnostic capture preference.",
  "tests_run": [],
  "known_issues": [],
  "completed_at": "2026-09-06T21:30:00Z"
}
```

## 22.5 Process status values

```text
SUCCEEDED
FAILED
TIMED_OUT
CANCELLED
ABANDONED
```

## 22.6 Important rule

`SUCCEEDED` means the Codex process completed successfully.

It does not mean:

- the milestone is valid;
- tests are trusted;
- the change should be committed;
- the PR should be approved.

Syntra must inspect the actual worktree.

---

# 23. WorkspaceRequest

## 23.1 Purpose

Requests creation or recovery of a controlled milestone workspace.

## 23.2 Producer

Orchestration Engine.

## 23.3 Consumer

Workspace Manager.

## 23.4 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "base_branch": "main",
  "branch_name": "syntra/m04-diagnostic-capture",
  "expected_base_sha": "def456"
}
```

---

# 24. WorkspaceDescriptor

## 24.1 Purpose

Describes an active local Git workspace.

## 24.2 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "repository_path": "/var/lib/syntra-build/projects/flowtrack/repo.git",
  "worktree_path": "/var/lib/syntra-build/projects/flowtrack/worktrees/m04",
  "branch_name": "syntra/m04-diagnostic-capture",
  "base_sha": "def456"
}
```

---

# 25. ChangeSet

## 25.1 Purpose

Represents Syntra's deterministic view of filesystem changes after a Codex run.

## 25.2 Producer

Git Manager.

## 25.3 Consumer

Validation layer and Orchestration Engine.

## 25.4 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "branch_name": "syntra/m04-diagnostic-capture",
  "base_sha": "def456",
  "changed_files": [],
  "added_files": [],
  "deleted_files": [],
  "diff_hash": "sha256:...",
  "is_empty": false
}
```

## 25.5 Optional fields

- line counts;
- binary-file flags;
- protected-file flags;
- secret-scan results;
- policy violations.

---

# 26. CommitRequest

## 26.1 Purpose

Requests creation of a Git commit from an already validated change set.

## 26.2 Producer

Orchestration Engine.

## 26.3 Consumer

Git Manager.

## 26.4 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "worktree_path": "...",
  "expected_diff_hash": "sha256:...",
  "commit_message": "M4: implement diagnostic capture optimisation"
}
```

The Git Manager must refuse the commit if the current diff no longer matches the expected diff hash.

---

# 27. CommitResult

## 27.1 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "commit_sha": "abc123",
  "branch_name": "syntra/m04-diagnostic-capture",
  "committed_at": "2026-09-06T21:35:00Z"
}
```

---

# 28. PushResult

## 28.1 Purpose

Represents the result of pushing a controlled branch to GitHub.

## 28.2 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "branch_name": "syntra/m04-diagnostic-capture",
  "commit_sha": "abc123",
  "remote": "origin",
  "status": "SUCCEEDED"
}
```

---

# 29. RepositoryProvisionRequest

## 29.1 Purpose

Requests creation and initial configuration of a generated-project GitHub repository.

## 29.2 Producer

Orchestration Engine.

## 29.3 Consumer

GitHub Adapter.

## 29.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "repository_name": "flowtrack",
  "visibility": "public",
  "default_branch": "main",
  "description": "..."
}
```

## 29.5 Validation rules

- project design must be approved;
- visibility must match approved project data;
- repository name must be validated;
- existing repository conflicts must be detected before create.

---

# 30. RepositoryDescriptor

## 30.1 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "github_repository_id": 123456,
  "owner": "example",
  "name": "flowtrack",
  "full_name": "example/flowtrack",
  "visibility": "public",
  "default_branch": "main",
  "web_url": "https://github.com/example/flowtrack"
}
```

---

# 31. PullRequestCreateRequest

## 31.1 Purpose

Requests creation of the milestone's real GitHub PR.

## 31.2 Producer

Orchestration Engine.

## 31.3 Consumer

GitHub Adapter.

## 31.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "repository_id": 123456,
  "head_branch": "syntra/m04-diagnostic-capture",
  "base_branch": "main",
  "head_sha": "abc123",
  "title": "M4: Diagnostic Capture Optimisation",
  "body": "..."
}
```

## 31.5 Idempotency

Before creating a PR, the GitHub Adapter must check whether an open PR already exists for the same:

- repository;
- head branch;
- milestone.

If one exists, it should be returned rather than duplicated.

---

# 32. PullRequestDescriptor

## 32.1 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "repository_id": 123456,
  "pull_request_number": 12,
  "state": "OPEN",
  "head_branch": "syntra/m04-diagnostic-capture",
  "base_branch": "main",
  "head_sha": "abc123",
  "web_url": "..."
}
```

## 32.2 State values

```text
OPEN
MERGED
CLOSED
```

---

# 33. CIStatus

## 33.1 Purpose

Represents the current non-terminal state of required CI checks.

## 33.2 Producer

CI Monitor.

## 33.3 Consumer

Status Service and Orchestration Engine.

## 33.4 Example

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "pull_request_number": 12,
  "head_sha": "abc123",
  "overall_status": "RUNNING",
  "required_checks": [
    {
      "name": "windows",
      "status": "PASSED"
    },
    {
      "name": "macos",
      "status": "RUNNING"
    }
  ]
}
```

## 33.5 Overall status values

```text
QUEUED
RUNNING
PASSED
FAILED
CANCELLED
UNKNOWN
```

---

# 34. CIResult

## 34.1 Purpose

Represents a terminal CI outcome for a PR head.

## 34.2 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "pull_request_number": 12,
  "head_sha": "abc123",
  "result": "FAILED",
  "checks": [],
  "failure_classification": "IMPLEMENTATION"
}
```

## 34.3 Failure classifications

```text
IMPLEMENTATION
TEST
CONFIGURATION
TRANSIENT_INFRASTRUCTURE
EXTERNAL_DEPENDENCY
UNKNOWN
```

## 34.4 Important rule

A CI result is valid only for the exact `head_sha`.

A new commit invalidates the prior CI result for merge purposes.

---

# 35. MergeRequest

## 35.1 Purpose

Requests a privileged GitHub merge after Gatekeeper approval.

## 35.2 Producer

Orchestration Engine.

## 35.3 Consumer

GitHub Adapter.

## 35.4 Required fields

```json
{
  "interface_version": "1.0",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "repository_id": 123456,
  "pull_request_number": 12,
  "expected_head_sha": "abc123",
  "merge_strategy": "SQUASH",
  "gatekeeper_result_id": "gatecheck_..."
}
```

## 35.5 Merge strategy values

```text
SQUASH
MERGE
REBASE
```

The initial preferred default is `SQUASH`.

---

# 36. MergeResult

## 36.1 Required fields

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "pull_request_number": 12,
  "status": "MERGED",
  "merge_commit_sha": "fed789",
  "merged_at": "2026-09-06T21:50:00Z"
}
```

## 36.2 Status values

```text
MERGED
NOT_MERGED
CONFLICT
REJECTED
UNKNOWN
```

The Orchestration Engine must still perform merge verification before marking the milestone complete.

---

# 37. WorkflowEvent

## 37.1 Purpose

Represents an internal domain event that may cause state evaluation.

## 37.2 Producer

Any validated Syntra component.

## 37.3 Consumer

Orchestration Engine.

## 37.4 Required fields

```json
{
  "interface_version": "1.0",
  "event_id": "evt_...",
  "correlation_id": "01J...",
  "event_type": "CI_PASSED",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "occurred_at": "2026-09-06T21:40:00Z",
  "payload": {}
}
```

## 37.5 Event types

The initial event vocabulary should align with `STATE_MACHINE.md`, including:

```text
PROJECT_CREATED
DESIGN_STARTED
DESIGN_DRAFT_READY
DESIGN_APPROVED
DESIGN_CHANGES_REQUESTED
REPOSITORY_CREATED
REPOSITORY_VERIFIED
MILESTONE_ACTIVATED
ARCHITECT_TASK_READY
WORKSPACE_READY
CODEX_COMPLETED
VALIDATION_PASSED
VALIDATION_FAILED
COMMIT_CREATED
BRANCH_PUSHED
PR_CREATED
CI_PASSED
CI_FAILED
ARCHITECT_APPROVED
ARCHITECT_CHANGES_REQUIRED
HUMAN_DECISION_REQUESTED
HUMAN_DECISION_RECEIVED
HUMAN_TEST_REQUESTED
HUMAN_TEST_PASSED
HUMAN_TEST_FAILED
MERGE_REQUESTED
MERGE_SUCCEEDED
MERGE_VERIFIED
PROJECT_PAUSED
PROJECT_RESUMED
PROJECT_CANCELLED
RETRY_EXHAUSTED
SYSTEM_RECOVERED
```

Events should be immutable once persisted.

---

# 38. JobDescriptor

## 38.1 Purpose

Represents one schedulable or running unit of work.

## 38.2 Required fields

```json
{
  "interface_version": "1.0",
  "job_id": "job_...",
  "correlation_id": "01J...",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "job_type": "CODEX_RUN",
  "state": "QUEUED",
  "attempt_number": 1,
  "max_attempts": 3,
  "created_at": "2026-09-06T21:00:00Z"
}
```

## 38.3 Job types

Examples:

```text
ARCHITECT_DESIGN
ARCHITECT_TASK
ARCHITECT_REVIEW
CODEX_RUN
REPOSITORY_PROVISION
GIT_COMMIT
GIT_PUSH
PR_CREATE
CI_RECONCILE
PR_MERGE
NOTIFICATION_SEND
RECOVERY_RECONCILE
```

---

# 39. ErrorRecord

## 39.1 Purpose

Provides a normalised error contract across all components.

## 39.2 Required fields

```json
{
  "interface_version": "1.0",
  "error_id": "err_...",
  "correlation_id": "01J...",
  "component": "github_adapter",
  "classification": "TRANSIENT",
  "code": "GITHUB_TIMEOUT",
  "message": "GitHub request timed out.",
  "retryable": true,
  "occurred_at": "2026-09-06T21:00:00Z"
}
```

## 39.3 Classification values

```text
TRANSIENT
RETRYABLE
WORKFLOW
VALIDATION
EXTERNAL
SECURITY
CONFIGURATION
FATAL
```

## 39.4 Optional fields

- project ID;
- milestone ID;
- job ID;
- external error reference;
- stack trace reference;
- user-safe summary.

Secrets must never appear in the persisted error message.

---

# 40. RecoveryObservation

## 40.1 Purpose

Represents an observed external fact during restart reconciliation.

## 40.2 Producer

Recovery Manager and adapters.

## 40.3 Consumer

Orchestration Engine.

## 40.4 Example

```json
{
  "interface_version": "1.0",
  "project_id": "proj_flowtrack",
  "milestone_id": "ms_m4",
  "observation_type": "PR_EXISTS",
  "observed_at": "2026-09-06T21:00:00Z",
  "details": {
    "pull_request_number": 12,
    "head_sha": "abc123"
  }
}
```

Recovery observations do not directly mutate state.

They are evidence used by the Orchestration Engine to select a safe transition.

---

# 41. Adapter Interface Requirements

Every external adapter should provide a predictable contract in four areas.

## 41.1 Request validation

Reject malformed requests before calling the external service.

## 41.2 Response normalisation

Convert provider-specific responses into Syntra domain objects.

## 41.3 Error normalisation

Convert provider-specific exceptions into `ErrorRecord`.

## 41.4 Reconciliation support

Provide read operations that allow Recovery Manager to determine whether a prior side effect actually occurred.

---

# 42. Architect Adapter Interface

The Architect Adapter should expose logical operations similar to:

```text
continue_design()
generate_specification()
generate_task()
review_pull_request()
analyse_change_request()
```

The precise Python method names may differ.

The important requirement is that core orchestration code interacts with domain contracts rather than raw provider messages.

---

# 43. Codex Adapter Interface

The Codex Runner should expose logical operations similar to:

```text
run_task()
terminate_run()
inspect_run()
```

The runner must return:

- process status;
- captured summary;
- logs reference;
- timing information.

It must not return GitHub state.

---

# 44. Git Adapter Interface

The Git Manager should expose deterministic operations such as:

```text
clone_or_fetch()
create_worktree()
inspect_changes()
commit_changes()
push_branch()
remove_worktree()
verify_commit()
```

Each operation should verify project and repository identity before execution.

---

# 45. GitHub Adapter Interface

The GitHub Adapter should expose logical operations such as:

```text
create_repository()
get_repository()
configure_repository()
create_or_get_pull_request()
get_pull_request()
get_ci_status()
merge_pull_request()
verify_merge()
delete_branch()
```

Create operations should be safe to repeat through reconciliation.

---

# 46. Messaging Adapter Interface

The Messaging Gateway should expose logical operations such as:

```text
receive_updates()
send_message()
send_gate_request()
send_status()
```

The orchestrator must not depend on Telegram-specific structures.

---

# 47. Validation Boundary

Validation occurs at multiple layers.

## Adapter validation

Checks protocol correctness.

Example:

- GitHub returned a valid PR object.

## Domain validation

Checks identity and state consistency.

Example:

- the PR belongs to the expected project and milestone.

## Gatekeeper validation

Checks privileged-action policy.

Example:

- merge is currently permitted.

These layers must remain distinct.

---

# 48. Interface Versioning

Every durable structured interface should include:

```text
interface_version
```

Initial version:

```text
1.0
```

Versioning is necessary because:

- persisted jobs may survive upgrades;
- recovery may process older records;
- AI structured-output schemas may evolve;
- adapters may change independently.

Breaking contract changes should require a major interface version increment.

---

# 49. Forward Compatibility

Consumers should generally:

- reject unknown required enum values;
- tolerate unknown optional fields;
- preserve raw provider references separately where useful;
- never silently reinterpret changed semantics.

---

# 50. Security Requirements for Interfaces

Interfaces must never carry secrets unless the receiving component explicitly requires them.

In particular:

- Architect requests should not contain GitHub tokens;
- Codex requests should not contain GitHub tokens;
- Telegram messages should not contain secrets;
- error objects should redact credentials;
- logs should record secret references rather than values.

Detailed security rules belong in `SECURITY.md`.

---

# 51. Audit Requirements

The following interfaces should normally be persisted or auditable:

- human commands;
- human gate requests and responses;
- Architect tasks;
- Architect reviews;
- Codex runs;
- change-set validation results;
- commits;
- PR creation;
- CI results;
- merge requests;
- merge results;
- workflow events;
- state transitions;
- errors.

This creates an end-to-end development history.

---

# 52. Example Milestone Interaction

A complete milestone interaction may look like:

```text
Orchestrator
    │
    ├── ArchitectTaskRequest
    │        ↓
    │   Architect Adapter
    │        ↓
    │   ArchitectTask
    │
    ├── WorkspaceRequest
    │        ↓
    │   Workspace Manager
    │        ↓
    │   WorkspaceDescriptor
    │
    ├── CodexRunRequest
    │        ↓
    │   Codex Runner
    │        ↓
    │   CodexRunResult
    │
    ├── inspect worktree
    │        ↓
    │   ChangeSet
    │
    ├── CommitRequest
    │        ↓
    │   CommitResult
    │
    ├── push
    │        ↓
    │   PushResult
    │
    ├── PullRequestCreateRequest
    │        ↓
    │   PullRequestDescriptor
    │
    ├── CIStatus / CIResult
    │
    ├── ArchitectReviewRequest
    │        ↓
    │   ArchitectReview
    │
    ├── Gatekeeper validation
    │
    ├── MergeRequest
    │        ↓
    │   MergeResult
    │
    └── MERGE_VERIFIED event
```

---

# 53. Example Rework Interaction

```text
ArchitectReview
verdict = CHANGES_REQUIRED
        ↓
WorkflowEvent:
ARCHITECT_CHANGES_REQUIRED
        ↓
ArchitectTask
task_type = REVIEW_REWORK
        ↓
CodexRunRequest
attempt = 2
        ↓
CodexRunResult
        ↓
new ChangeSet
        ↓
new CommitResult
        ↓
PushResult
        ↓
same PR, new head SHA
        ↓
new CIResult
        ↓
new ArchitectReviewRequest
```

The existing PR remains the same.

The reviewed SHA changes.

All prior CI and Architect approvals become stale for merge purposes.

---

# 54. Example Human Test Interaction

```text
ArchitectReview
verdict = HUMAN_TEST_REQUIRED
        ↓
HumanGateRequest
gate_type = HUMAN_TEST
        ↓
OutboundMessage
        ↓
Human performs test
        ↓
InboundMessage
        ↓
HumanGateResponse
response = PASS
        ↓
HUMAN_TEST_PASSED event
        ↓
MERGE_READY evaluation
```

A `FAIL` response instead produces rework.

---

# 55. Interface Invariants

The following invariants must always hold:

1. Every workflow-changing message has a correlation ID.
2. Every milestone-scoped message identifies the milestone explicitly.
3. Every Architect approval identifies the reviewed SHA.
4. Every CI result identifies the tested SHA.
5. Every merge request identifies the expected head SHA.
6. Every human response identifies an unresolved gate.
7. Every Codex run identifies an assigned worktree.
8. Codex results do not contain authoritative GitHub actions.
9. GitHub create operations are idempotent or reconciled.
10. Interface consumers validate project identity before acting.
11. Unknown state-changing enum values are rejected.
12. Human silence is never interpreted as approval.

---

# 56. Interface Completion Criteria

The interface design is sufficiently complete when:

- every external integration is behind an adapter;
- every workflow-changing AI response has a structured contract;
- every privileged Git/GitHub action has an explicit request/result contract;
- every human gate can be correlated to a response;
- every status query has a defined read-only response;
- every milestone interaction carries project, milestone, and SHA identity where relevant;
- retryable operations can be reconciled;
- recovery can inspect external state without replaying blindly;
- raw provider payloads are isolated from the domain layer;
- the contracts provide enough information to design the persistent data model.

The next design document should be `DATA_MODEL.md`, which will map these interfaces and state machines into concrete persistent entities and relationships.
