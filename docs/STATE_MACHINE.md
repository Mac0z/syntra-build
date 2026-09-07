# Syntra Build — State Machine

## 1. Purpose

This document defines the formal state model used by Syntra Build.

It translates the behaviour described in `WORKFLOW.md` into explicit states and permitted transitions.

The purpose of the state machine is to ensure that:

- every project has a single authoritative current state;
- every active milestone has a single authoritative current state;
- external work such as Architect calls, Codex execution and GitHub CI can be tracked independently;
- human decisions and testing are represented as durable workflow objects;
- invalid transitions are rejected;
- retries are bounded and visible;
- system restart does not lose workflow position;
- status queries can be answered deterministically;
- multiple projects can execute concurrently without interfering with one another.

Syntra Build owns and persists all state transitions.

AI agents do not directly change workflow state.

---

# 2. State Model Overview

Syntra Build uses four related state machines:

```text
Project State
    │
    ├── Current Milestone State
    │       │
    │       ├── Execution Jobs
    │       │
    │       └── Human Gates
    │
    └── Project-level Human Gates
```

The four state domains are:

1. **Project State**  
   Describes the overall lifecycle of a project.

2. **Milestone State**  
   Describes the lifecycle of the currently active implementation milestone.

3. **Job State**  
   Describes individual pieces of asynchronous work such as Architect calls, Codex runs, GitHub operations or CI monitoring.

4. **Human Gate State**  
   Describes any explicit human approval, decision or test request.

Project and milestone states describe business workflow.

Job states describe execution.

Human-gate states describe user intervention.

They must remain separate.

For example, a project may remain in:

```text
BUILDING
```

while its current milestone is:

```text
CI_RUNNING
```

and its CI-monitoring job is:

```text
WAITING_EXTERNAL
```

---

# 3. State Transition Principles

## 3.1 Only Syntra changes authoritative state

Architect, Codex, GitHub and Telegram may produce events.

Those events are interpreted by Syntra.

For example:

```text
Architect response:
APPROVE
```

does not directly change a milestone to `COMPLETE`.

Instead:

```text
Architect response
    ↓
Syntra validates response
    ↓
Milestone → ARCHITECT_APPROVED
    ↓
Syntra evaluates remaining gates
```

---

## 3.2 Every state transition is persisted

A state transition must be committed to durable storage before the next workflow action begins.

Each transition should record:

- previous state;
- new state;
- timestamp;
- reason;
- triggering event;
- actor;
- associated project;
- associated milestone where applicable;
- correlation identifier;
- relevant job or gate identifier.

---

## 3.3 State transitions must be validated

Transitions not explicitly allowed by this document must be rejected.

For example:

```text
CODING → MERGED
```

must not be possible.

The valid path must pass through validation, PR creation, CI and review.

---

## 3.4 Actions must be idempotent where practical

Where Syntra performs external actions, it must check whether that action has already succeeded before repeating it.

Examples:

- repository creation;
- branch push;
- PR creation;
- PR merge;
- human-gate creation.

This is especially important during restart recovery.

---

# 4. Project States

The project state describes the overall lifecycle of a software project.

The initial project states are:

```text
NEW
DESIGNING
DESIGN_APPROVAL
PROVISIONING
READY
BUILDING
WAITING_HUMAN
PAUSED
BLOCKED
COMPLETING
COMPLETE
FAILED
CANCELLED
```

---

# 5. Project State Definitions

## 5.1 NEW

The project record has been created but formal design activity has not yet started.

Typical activities:

- project-name validation;
- messaging-thread creation;
- initial metadata storage.

Expected duration should normally be very short.

### Valid next states

```text
NEW → DESIGNING
NEW → CANCELLED
NEW → FAILED
```

---

## 5.2 DESIGNING

The project is in conversational design.

The human and Architect are defining:

- scope;
- requirements;
- architecture;
- technology choices;
- repository visibility;
- milestones;
- acceptance criteria.

No generated-project GitHub repository exists yet.

### Valid next states

```text
DESIGNING → DESIGN_APPROVAL
DESIGNING → PAUSED
DESIGNING → BLOCKED
DESIGNING → CANCELLED
DESIGNING → FAILED
```

---

## 5.3 DESIGN_APPROVAL

A draft `SPEC.md` and `AGENTS.md` exist and are waiting for explicit human approval.

The project must not enter provisioning until approval has been recorded.

### Valid next states

```text
DESIGN_APPROVAL → DESIGNING
DESIGN_APPROVAL → PROVISIONING
DESIGN_APPROVAL → PAUSED
DESIGN_APPROVAL → CANCELLED
DESIGN_APPROVAL → FAILED
```

`DESIGN_APPROVAL → DESIGNING` occurs when the human requests changes.

---

## 5.4 PROVISIONING

Syntra is creating and configuring the GitHub repository.

Typical work includes:

- creating repository;
- applying approved visibility;
- creating `main`;
- committing design baseline;
- configuring repository settings;
- configuring GitHub Actions where applicable;
- verifying repository state.

Repositories are public by default unless private visibility was explicitly approved during design.

### Valid next states

```text
PROVISIONING → READY
PROVISIONING → BLOCKED
PROVISIONING → FAILED
PROVISIONING → CANCELLED
```

---

## 5.5 READY

The project repository exists and is ready for implementation.

No milestone is currently executing.

This state may be transient because Syntra will normally activate the first available milestone automatically.

### Valid next states

```text
READY → BUILDING
READY → PAUSED
READY → BLOCKED
READY → COMPLETING
READY → CANCELLED
READY → FAILED
```

`READY → COMPLETING` is possible for a project with no implementation milestones, though this is expected to be rare.

---

## 5.6 BUILDING

At least one milestone remains incomplete and the project is actively progressing through implementation.

The detailed current activity is represented by the milestone state.

Examples:

```text
Project: BUILDING
Milestone: CODING
```

or:

```text
Project: BUILDING
Milestone: ARCHITECT_REVIEW
```

### Valid next states

```text
BUILDING → WAITING_HUMAN
BUILDING → READY
BUILDING → PAUSED
BUILDING → BLOCKED
BUILDING → COMPLETING
BUILDING → FAILED
BUILDING → CANCELLED
```

`BUILDING → READY` may occur briefly between milestones.

---

## 5.7 WAITING_HUMAN

The project cannot continue until one or more human gates are resolved.

Examples:

- design decision;
- human test;
- manual verification;
- privileged recovery choice.

At least one unresolved gate must exist while the project is in this state.

### Valid next states

```text
WAITING_HUMAN → DESIGNING
WAITING_HUMAN → BUILDING
WAITING_HUMAN → COMPLETING
WAITING_HUMAN → PAUSED
WAITING_HUMAN → BLOCKED
WAITING_HUMAN → CANCELLED
WAITING_HUMAN → FAILED
```

The destination depends on the workflow that created the gate.

---

## 5.8 PAUSED

The project has been intentionally paused.

A pause prevents new workflow work from starting.

Existing safe atomic operations may finish before the pause fully takes effect.

Paused projects retain:

- active milestone;
- open PR;
- retry counters;
- outstanding gates;
- current workflow position.

### Valid next states

```text
PAUSED → DESIGNING
PAUSED → DESIGN_APPROVAL
PAUSED → READY
PAUSED → BUILDING
PAUSED → WAITING_HUMAN
PAUSED → BLOCKED
PAUSED → CANCELLED
```

The resume target must be the state that logically existed before the pause.

Syntra should persist a `resume_state`.

---

## 5.9 BLOCKED

Syntra cannot safely make automatic progress, but the project is not considered permanently failed.

Examples:

- external dependency unavailable for an extended period;
- repository policy conflict;
- unresolved architectural inconsistency;
- unsupported environmental requirement;
- repeated CI infrastructure failure.

A blocked project should normally generate a human notification.

### Valid next states

```text
BLOCKED → DESIGNING
BLOCKED → READY
BLOCKED → BUILDING
BLOCKED → WAITING_HUMAN
BLOCKED → PAUSED
BLOCKED → FAILED
BLOCKED → CANCELLED
```

---

## 5.10 COMPLETING

All implementation milestones are complete.

Syntra is executing final project-level acceptance.

Possible activities:

- final CI;
- release creation;
- final build;
- final human acceptance;
- repository verification;
- documentation verification.

### Valid next states

```text
COMPLETING → WAITING_HUMAN
COMPLETING → COMPLETE
COMPLETING → BLOCKED
COMPLETING → FAILED
COMPLETING → CANCELLED
```

---

## 5.11 COMPLETE

The project has satisfied its completion definition.

This is a terminal state.

No implementation work should be scheduled automatically.

A completed project remains queryable.

### Valid next states

None during normal operation.

Future versions may support explicit project reopening.

---

## 5.12 FAILED

The project encountered a failure that Syntra has classified as terminal or requiring explicit administrative recovery.

Examples:

- persistent database integrity problem;
- unrecoverable repository mismatch;
- repeated invalid state transition;
- retry exhaustion followed by escalation failure.

`FAILED` is terminal for normal automatic workflow.

A future administrative recovery mechanism may permit explicit reopening.

---

## 5.13 CANCELLED

The human explicitly cancelled the project.

Project records and history remain intact.

The GitHub repository is not automatically deleted.

This is a terminal state.

---

# 6. Project State Diagram

```text
                     ┌─────────────┐
                     │     NEW     │
                     └──────┬──────┘
                            │
                            ▼
                     ┌─────────────┐
                     │  DESIGNING  │
                     └──────┬──────┘
                            │
                            ▼
                  ┌──────────────────┐
                  │ DESIGN_APPROVAL  │
                  └───────┬──────────┘
                          │ approved
                          ▼
                   ┌──────────────┐
                   │ PROVISIONING │
                   └──────┬───────┘
                          │
                          ▼
                      ┌───────┐
                      │ READY │
                      └───┬───┘
                          │
                          ▼
                    ┌──────────┐
        ┌──────────▶│ BUILDING │◀──────────┐
        │           └────┬─────┘           │
        │                │                 │
        │         human gate               │
        │                ▼                 │
        │        ┌───────────────┐         │
        └────────│ WAITING_HUMAN │─────────┘
                 └───────────────┘
                          │
                     milestones done
                          ▼
                   ┌────────────┐
                   │ COMPLETING │
                   └─────┬──────┘
                         │
                         ▼
                    ┌──────────┐
                    │ COMPLETE │
                    └──────────┘
```

`PAUSED`, `BLOCKED`, `FAILED` and `CANCELLED` may be entered from several stages and are omitted from the simplified diagram.

---

# 7. Milestone States

Each active milestone has its own state machine.

The initial milestone states are:

```text
PENDING
READY
PREPARING_TASK
PREPARING_WORKSPACE
CODING
VALIDATING_CHANGES
COMMITTING
PUSHING
PR_CREATING
CI_RUNNING
CI_REWORK
ARCHITECT_REVIEW
REVIEW_REWORK
HUMAN_DECISION
HUMAN_TEST
MERGE_READY
MERGING
MERGE_VERIFY
COMPLETE
BLOCKED
FAILED
CANCELLED
```

---

# 8. Milestone State Definitions

## 8.1 PENDING

The milestone exists in the approved project plan but cannot yet begin.

Typical reason:

- preceding dependency is incomplete.

### Valid next states

```text
PENDING → READY
PENDING → CANCELLED
```

---

## 8.2 READY

All dependencies are satisfied.

The milestone may be scheduled.

### Valid next states

```text
READY → PREPARING_TASK
READY → BLOCKED
READY → CANCELLED
```

---

## 8.3 PREPARING_TASK

Syntra has asked the Architect to prepare the implementation task.

### Valid next states

```text
PREPARING_TASK → PREPARING_WORKSPACE
PREPARING_TASK → HUMAN_DECISION
PREPARING_TASK → BLOCKED
PREPARING_TASK → FAILED
```

---

## 8.4 PREPARING_WORKSPACE

Syntra is:

- updating the local repository;
- creating the milestone branch;
- creating the Git worktree;
- recording workspace metadata.

### Valid next states

```text
PREPARING_WORKSPACE → CODING
PREPARING_WORKSPACE → BLOCKED
PREPARING_WORKSPACE → FAILED
```

---

## 8.5 CODING

Codex is actively implementing or revising the milestone.

The Codex attempt number must be recorded.

### Valid next states

```text
CODING → VALIDATING_CHANGES
CODING → BLOCKED
CODING → FAILED
```

---

## 8.6 VALIDATING_CHANGES

Syntra is performing deterministic validation against the Codex worktree.

### Valid next states

```text
VALIDATING_CHANGES → COMMITTING
VALIDATING_CHANGES → CODING
VALIDATING_CHANGES → HUMAN_DECISION
VALIDATING_CHANGES → BLOCKED
VALIDATING_CHANGES → FAILED
```

`VALIDATING_CHANGES → CODING` occurs when a correctable validation failure is returned to Codex.

---

## 8.7 COMMITTING

Syntra is creating a Git commit from validated changes.

### Valid next states

```text
COMMITTING → PUSHING
COMMITTING → BLOCKED
COMMITTING → FAILED
```

---

## 8.8 PUSHING

Syntra is pushing the milestone branch to GitHub.

### Valid next states

For a new PR:

```text
PUSHING → PR_CREATING
```

For an existing PR:

```text
PUSHING → CI_RUNNING
```

Other possibilities:

```text
PUSHING → BLOCKED
PUSHING → FAILED
```

---

## 8.9 PR_CREATING

Syntra is creating the milestone's GitHub pull request.

### Valid next states

```text
PR_CREATING → CI_RUNNING
PR_CREATING → BLOCKED
PR_CREATING → FAILED
```

A milestone must have at most one active implementation PR.

---

## 8.10 CI_RUNNING

Required GitHub Actions checks are running or waiting to run.

No Codex worker is required while the project is in this state.

### Valid next states

```text
CI_RUNNING → ARCHITECT_REVIEW
CI_RUNNING → CI_REWORK
CI_RUNNING → BLOCKED
CI_RUNNING → FAILED
```

---

## 8.11 CI_REWORK

CI has failed and the failure has been classified as requiring implementation changes.

Syntra prepares a correction task for Codex.

### Valid next states

```text
CI_REWORK → CODING
CI_REWORK → HUMAN_DECISION
CI_REWORK → BLOCKED
CI_REWORK → FAILED
```

---

## 8.12 ARCHITECT_REVIEW

Required CI has passed and the Architect is reviewing the current PR head.

The reviewed commit SHA must be persisted.

### Valid next states

```text
ARCHITECT_REVIEW → MERGE_READY
ARCHITECT_REVIEW → REVIEW_REWORK
ARCHITECT_REVIEW → HUMAN_TEST
ARCHITECT_REVIEW → HUMAN_DECISION
ARCHITECT_REVIEW → BLOCKED
ARCHITECT_REVIEW → FAILED
```

---

## 8.13 REVIEW_REWORK

Architect review returned `CHANGES_REQUIRED`.

Findings have been recorded and Codex needs to revise the implementation.

### Valid next states

```text
REVIEW_REWORK → CODING
REVIEW_REWORK → HUMAN_DECISION
REVIEW_REWORK → BLOCKED
REVIEW_REWORK → FAILED
```

---

## 8.14 HUMAN_DECISION

The milestone cannot continue until an explicit human decision has been received.

At least one unresolved decision gate must exist.

### Valid next states

Depending on the decision:

```text
HUMAN_DECISION → PREPARING_TASK
HUMAN_DECISION → CODING
HUMAN_DECISION → ARCHITECT_REVIEW
HUMAN_DECISION → MERGE_READY
HUMAN_DECISION → BLOCKED
HUMAN_DECISION → CANCELLED
```

---

## 8.15 HUMAN_TEST

Automated checks and/or Architect review require human validation.

At least one unresolved human-test gate must exist.

### Valid next states

Human test passed:

```text
HUMAN_TEST → MERGE_READY
```

Human test failed:

```text
HUMAN_TEST → REVIEW_REWORK
```

Other possibilities:

```text
HUMAN_TEST → HUMAN_DECISION
HUMAN_TEST → BLOCKED
HUMAN_TEST → CANCELLED
```

---

## 8.16 MERGE_READY

All required gates appear satisfied.

No merge has yet occurred.

Syntra must execute the final deterministic gatekeeper checks in this state.

### Valid next states

```text
MERGE_READY → MERGING
MERGE_READY → CI_RUNNING
MERGE_READY → ARCHITECT_REVIEW
MERGE_READY → HUMAN_TEST
MERGE_READY → HUMAN_DECISION
MERGE_READY → BLOCKED
MERGE_READY → FAILED
```

The fallback states exist because the PR may have changed after approval.

For example:

- CI status may have become stale;
- the PR head may have changed;
- a new required gate may have appeared.

---

## 8.17 MERGING

Syntra is performing the GitHub merge.

### Valid next states

```text
MERGING → MERGE_VERIFY
MERGING → BLOCKED
MERGING → FAILED
```

---

## 8.18 MERGE_VERIFY

Syntra verifies that the expected PR was merged into the expected base branch and that the resulting commit is present.

### Valid next states

```text
MERGE_VERIFY → COMPLETE
MERGE_VERIFY → BLOCKED
MERGE_VERIFY → FAILED
```

---

## 8.19 COMPLETE

The milestone has been successfully merged and verified.

This is the normal terminal state for a milestone.

---

## 8.20 BLOCKED

Automatic milestone progression is not currently safe.

The milestone remains recoverable.

Examples:

- persistent external failure;
- repository mismatch;
- architectural conflict;
- retry exhaustion awaiting escalation.

### Valid next states

The milestone may return to the most appropriate previous workflow state after resolution.

The exact recovery target must be stored explicitly.

---

## 8.21 FAILED

The milestone has encountered a terminal failure.

No automatic work should continue.

Project-level policy determines whether this makes the whole project `FAILED` or `BLOCKED`.

---

## 8.22 CANCELLED

The milestone was explicitly cancelled.

No further implementation work should occur.

---

# 9. Milestone Happy Path

The normal milestone flow is:

```text
PENDING
   ↓
READY
   ↓
PREPARING_TASK
   ↓
PREPARING_WORKSPACE
   ↓
CODING
   ↓
VALIDATING_CHANGES
   ↓
COMMITTING
   ↓
PUSHING
   ↓
PR_CREATING
   ↓
CI_RUNNING
   ↓
ARCHITECT_REVIEW
   ↓
MERGE_READY
   ↓
MERGING
   ↓
MERGE_VERIFY
   ↓
COMPLETE
```

---

# 10. CI Rework Loop

```text
CI_RUNNING
    │
    │ failure
    ▼
CI_REWORK
    ↓
CODING
    ↓
VALIDATING_CHANGES
    ↓
COMMITTING
    ↓
PUSHING
    ↓
CI_RUNNING
```

This loop must be bounded by retry policy.

---

# 11. Architect Rework Loop

```text
ARCHITECT_REVIEW
      │
      │ CHANGES_REQUIRED
      ▼
 REVIEW_REWORK
      ↓
    CODING
      ↓
VALIDATING_CHANGES
      ↓
  COMMITTING
      ↓
   PUSHING
      ↓
 CI_RUNNING
      ↓
ARCHITECT_REVIEW
```

Each review attempt must reference the specific PR head SHA being reviewed.

---

# 12. Human Test Loop

```text
ARCHITECT_REVIEW
       │
       │ HUMAN_TEST_REQUIRED
       ▼
   HUMAN_TEST
       │
   ┌───┴──────────┐
   │              │
 PASS           FAIL
   │              │
   ▼              ▼
MERGE_READY   REVIEW_REWORK
```

---

# 13. Human Decision Loop

A human decision may interrupt several workflow stages.

Example:

```text
PREPARING_TASK
      ↓
HUMAN_DECISION
      ↓
PREPARING_TASK
```

or:

```text
ARCHITECT_REVIEW
      ↓
HUMAN_DECISION
      ↓
REVIEW_REWORK
```

The human gate must store a `resume_target` so Syntra knows where to continue once the decision is resolved.

---

# 14. Job States

Jobs represent executable units of work.

Examples include:

- Architect design request;
- Architect task-generation request;
- Architect PR review;
- Codex execution;
- Git commit operation;
- Git push;
- GitHub repository creation;
- PR creation;
- PR merge;
- CI reconciliation;
- Telegram notification.

The job states are:

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

# 15. Job State Definitions

## 15.1 QUEUED

The job exists and is waiting for scheduler capacity.

### Valid next states

```text
QUEUED → DISPATCHED
QUEUED → CANCELLED
```

---

## 15.2 DISPATCHED

The scheduler has allocated the job to a worker.

The external operation may not yet have started.

### Valid next states

```text
DISPATCHED → RUNNING
DISPATCHED → FAILED
DISPATCHED → CANCELLED
```

---

## 15.3 RUNNING

The job is actively executing.

Examples:

- Codex process active;
- Architect API request running;
- Git command executing.

### Valid next states

```text
RUNNING → WAITING_EXTERNAL
RUNNING → SUCCEEDED
RUNNING → RETRY_WAIT
RUNNING → FAILED
RUNNING → CANCELLED
```

---

## 15.4 WAITING_EXTERNAL

Syntra has initiated an external process and is waiting for completion.

Typical examples:

- GitHub Actions;
- asynchronous remote build;
- GitHub-side merge queue.

This state does not consume a scarce worker slot unless active polling is required.

### Valid next states

```text
WAITING_EXTERNAL → SUCCEEDED
WAITING_EXTERNAL → RETRY_WAIT
WAITING_EXTERNAL → FAILED
WAITING_EXTERNAL → CANCELLED
```

---

## 15.5 SUCCEEDED

The job completed successfully.

Terminal state.

---

## 15.6 RETRY_WAIT

The job failed in a retryable way and is waiting for its retry interval.

The job should store:

- attempt count;
- maximum attempts;
- next retry time;
- failure classification.

### Valid next states

```text
RETRY_WAIT → QUEUED
RETRY_WAIT → FAILED
RETRY_WAIT → CANCELLED
```

---

## 15.7 FAILED

The job has failed and no automatic retry remains.

Terminal state for the job.

The parent workflow determines whether to:

- create a replacement job;
- block the milestone;
- request human input;
- fail the project.

---

## 15.8 CANCELLED

The job was explicitly cancelled before successful completion.

Terminal state.

---

## 15.9 ABANDONED

Syntra discovered during recovery that an earlier job cannot safely be considered running or completed.

Examples:

- Codex process disappeared during a host crash;
- local process ID no longer exists;
- Architect request outcome is unknowable.

The workflow must reconcile state before creating replacement work.

`ABANDONED` is terminal for that job instance.

---

# 16. Human Gate Types

Human gates represent explicit user actions required before workflow can continue.

Initial gate types are:

```text
DESIGN_APPROVAL
PRODUCT_DECISION
TECHNICAL_DECISION
HUMAN_TEST
FINAL_ACCEPTANCE
RECOVERY_DECISION
```

---

# 17. Human Gate States

All human-gate types use the same basic state machine:

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

# 18. Human Gate State Definitions

## 18.1 PENDING

The gate has been created but notification has not yet been confirmed.

### Valid next states

```text
PENDING → NOTIFIED
PENDING → CANCELLED
```

---

## 18.2 NOTIFIED

The human request has been sent successfully through the messaging platform.

### Valid next states

```text
NOTIFIED → RESPONDED
NOTIFIED → EXPIRED
NOTIFIED → CANCELLED
```

---

## 18.3 RESPONDED

A user response has been associated with the gate.

The response has not yet been validated.

### Valid next states

```text
RESPONDED → VALIDATED
RESPONDED → NOTIFIED
RESPONDED → CANCELLED
```

`RESPONDED → NOTIFIED` occurs if clarification is required.

---

## 18.4 VALIDATED

Syntra has determined that the response is valid for the requested gate.

### Valid next states

```text
VALIDATED → RESOLVED
```

---

## 18.5 RESOLVED

The human gate is complete.

Terminal state.

The associated project or milestone may resume.

---

## 18.6 EXPIRED

The gate exceeded a configured time policy.

Expiration does not imply approval or rejection.

Depending on policy, Syntra may:

- send another notification;
- create an escalation;
- leave the project waiting;
- move the project to `BLOCKED`.

---

## 18.7 CANCELLED

The gate is no longer required.

Terminal state.

---

# 19. Design Approval Gate

A `DESIGN_APPROVAL` gate has expected outcomes:

```text
APPROVE
REQUEST_CHANGES
CANCEL_PROJECT
```

Expected transitions:

### APPROVE

```text
Project:
DESIGN_APPROVAL → PROVISIONING
```

### REQUEST_CHANGES

```text
Project:
DESIGN_APPROVAL → DESIGNING
```

### CANCEL_PROJECT

```text
Project:
DESIGN_APPROVAL → CANCELLED
```

---

# 20. Human Test Gate

A `HUMAN_TEST` gate has expected outcomes:

```text
PASS
FAIL
BLOCKED
```

### PASS

```text
Milestone:
HUMAN_TEST → MERGE_READY
```

### FAIL

```text
Milestone:
HUMAN_TEST → REVIEW_REWORK
```

The supplied test evidence should be sent to the Architect before the next Codex task is produced.

### BLOCKED

```text
Milestone:
HUMAN_TEST → BLOCKED
```

---

# 21. Status Projection

The user should not normally be shown raw internal state names alone.

Syntra should convert state into useful operational language.

Examples:

```text
Project state: BUILDING
Milestone state: CODING
```

User-facing status:

> FlowTrack is on M4 — Diagnostic Capture Optimisation. Codex is currently implementing the milestone.

---

```text
Project state: BUILDING
Milestone state: CI_RUNNING
```

User-facing status:

> FlowTrack M4 is waiting for GitHub Actions. Two of three required checks have completed.

---

```text
Project state: WAITING_HUMAN
Milestone state: HUMAN_TEST
```

User-facing status:

> FlowTrack M4 is waiting for you to test the latest macOS build.

---

# 22. Required Status Fields

For every project, Syntra should be able to derive:

- project name;
- project state;
- project status summary;
- active milestone;
- milestone state;
- milestone objective;
- current activity;
- whether local execution is active;
- current Codex attempt;
- active branch;
- active PR;
- current PR head SHA;
- CI status;
- latest Architect verdict;
- unresolved human gates;
- retry state;
- most recent error;
- next expected action;
- last state-change timestamp.

These fields provide the basis for Telegram status queries.

---

# 23. State and Activity Separation

State should remain relatively coarse and stable.

Short-lived detail belongs in an `activity` field.

For example:

```text
milestone_state = CI_RUNNING
activity = "Waiting for macOS package job"
```

or:

```text
milestone_state = CODING
activity = "Codex attempt 3 addressing Architect findings"
```

This avoids creating excessive states such as:

```text
CI_RUNNING_WINDOWS
CI_RUNNING_MAC
CI_RUNNING_LINUX
```

---

# 24. Retry Counters

Retries should be tracked by category rather than using one global number.

Suggested counters include:

```text
architect_api_attempts
codex_attempts
ci_transient_retries
ci_rework_cycles
architect_review_cycles
git_push_attempts
github_api_attempts
notification_attempts
```

Each category should have configurable limits.

Retry exhaustion must produce an explicit transition rather than silently stopping.

Typical result:

```text
current state → BLOCKED
```

or:

```text
current state → FAILED
```

depending on severity.

---

# 25. Project and Milestone Relationship

A project may have many milestones.

However, in the initial version:

> A project should have at most one actively executing implementation milestone at a time.

This avoids complex dependency races within a single repository.

Multiple projects may execute milestones concurrently.

Example:

```text
FlowTrack
  M4 → CI_RUNNING

PulseVault
  M2 → CODING

Spread Zeppelin
  M5 → HUMAN_TEST
```

This still provides parallel project development while keeping each project internally deterministic.

Future versions may support parallel independent milestones within one project if required.

---

# 26. Scheduler Eligibility

Not all states are eligible for active scheduling.

## States that may generate immediate work

Examples:

```text
Project READY
Milestone READY
Milestone PREPARING_TASK
Milestone PREPARING_WORKSPACE
Milestone CI_REWORK
Milestone REVIEW_REWORK
Milestone MERGE_READY
```

---

## States waiting for external completion

Examples:

```text
Milestone CI_RUNNING
Job WAITING_EXTERNAL
```

These should not consume Codex worker capacity.

---

## States waiting for the human

Examples:

```text
Project WAITING_HUMAN
Milestone HUMAN_TEST
Milestone HUMAN_DECISION
```

These consume no AI worker capacity unless the user responds.

---

## Terminal states

```text
COMPLETE
FAILED
CANCELLED
```

These generate no automatic work.

---

# 27. Pause Semantics

Pause is a project-level override.

When a project becomes `PAUSED`:

- no new jobs may begin;
- queued jobs for that project should be suspended;
- currently running safe atomic jobs may finish;
- Codex should not begin a new attempt;
- a PR should not be merged;
- CI already running may continue remotely;
- inbound status queries still work;
- human responses may still be recorded.

The previous operational state must be stored in:

```text
resume_state
```

and, where relevant:

```text
resume_milestone_state
```

Resume must restore the logically correct workflow state after reconciliation.

---

# 28. Cancellation Semantics

Cancellation is stronger than pause.

After project cancellation:

- no new jobs may be scheduled;
- active Codex processes should be terminated safely;
- no PR may be merged;
- outstanding human gates are cancelled;
- project history remains preserved;
- GitHub repository is preserved by default;
- repository deletion is never implied.

---

# 29. Recovery State Reconciliation

On Syntra startup, persisted states must be reconciled against reality.

Examples follow.

---

## 29.1 Persisted state: CODING

Syntra checks whether the Codex process is still running.

If not:

- existing job becomes `ABANDONED`;
- worktree is inspected;
- partial changes are preserved;
- Syntra determines whether to retry Codex or escalate.

---

## 29.2 Persisted state: PR_CREATING

Syntra queries GitHub.

If a matching PR already exists:

```text
PR_CREATING → CI_RUNNING
```

If no PR exists:

Syntra may safely retry creation.

---

## 29.3 Persisted state: CI_RUNNING

Syntra queries GitHub for the actual check status.

Possible reconciliation:

```text
still running → CI_RUNNING
passed        → ARCHITECT_REVIEW
failed        → CI_REWORK
```

---

## 29.4 Persisted state: MERGING

Syntra checks whether the PR was already merged.

If yes:

```text
MERGING → MERGE_VERIFY
```

If not, Syntra must determine whether the merge operation can safely be retried.

---

## 29.5 Persisted state: HUMAN_TEST

The human gate remains valid.

No special recovery action is required other than restoring messaging context.

---

# 30. Illegal Transition Examples

The following transitions must be rejected:

```text
DESIGNING → BUILDING
```

because repository provisioning and approval were bypassed.

```text
CODING → CI_RUNNING
```

because validation, commit and push were bypassed.

```text
CI_RUNNING → MERGING
```

because Architect review and gatekeeper checks were bypassed.

```text
ARCHITECT_REVIEW → COMPLETE
```

because merge has not occurred.

```text
HUMAN_TEST → COMPLETE
```

because successful test does not itself merge the PR.

```text
FAILED → BUILDING
```

without an explicit administrative recovery procedure.

---

# 31. Transition Guards

Some transitions require additional conditions.

Examples:

## `DESIGN_APPROVAL → PROVISIONING`

Requires:

- valid `SPEC.md`;
- valid `AGENTS.md`;
- explicit human approval;
- approved repository visibility.

---

## `CI_RUNNING → ARCHITECT_REVIEW`

Requires:

- all required CI checks complete;
- all required CI checks successful;
- PR head SHA recorded.

---

## `ARCHITECT_REVIEW → MERGE_READY`

Requires:

- Architect verdict `APPROVE`;
- reviewed SHA equals current PR head SHA;
- no unresolved major or critical findings.

---

## `HUMAN_TEST → MERGE_READY`

Requires:

- required test gate resolved as `PASS`;
- current tested artifact corresponds to current PR head or approved build.

---

## `MERGE_READY → MERGING`

Requires:

- PR open;
- expected repository;
- expected branch;
- required CI currently passing;
- Architect approval applies to current PR head;
- all required human gates resolved;
- project not paused;
- project not cancelled;
- no active blocking condition.

---

# 32. State Transition Events

Transitions should be caused by explicit events.

Examples:

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

Persisting events as well as current state will provide a useful audit trail.

---

# 33. Transition History

Every stateful entity should maintain transition history.

For example:

```text
FlowTrack / M4

10:03 READY
10:03 PREPARING_TASK
10:04 PREPARING_WORKSPACE
10:05 CODING
10:18 VALIDATING_CHANGES
10:18 COMMITTING
10:19 PUSHING
10:19 PR_CREATING
10:20 CI_RUNNING
10:27 ARCHITECT_REVIEW
10:31 REVIEW_REWORK
10:32 CODING
...
```

This history should be available for:

- debugging;
- audit;
- status summaries;
- performance analysis;
- future workflow optimisation.

---

# 34. Timeouts

States involving external systems should have timeout policies.

Examples:

- Architect request;
- Codex execution;
- GitHub API request;
- Git operation;
- CI run;
- messaging delivery.

A timeout must not automatically imply terminal failure.

It should generate a classified event such as:

```text
TIMEOUT_RETRYABLE
```

or:

```text
TIMEOUT_ESCALATED
```

Human gates should not have destructive automatic timeouts.

A user failing to reply must never be interpreted as approval.

---

# 35. State Invariants

The following invariants must always hold.

## Project invariants

- project name is unique within Syntra Build;
- a completed project has no active milestone;
- a cancelled project has no running jobs;
- a waiting-human project has at least one unresolved human gate.

## Milestone invariants

- at most one active implementation PR exists per milestone;
- a complete milestone references a verified merged PR;
- `CI_RUNNING` requires an active PR;
- `ARCHITECT_REVIEW` requires successful required CI;
- `MERGE_READY` requires Architect approval;
- `MERGING` requires successful gatekeeper validation.

## Job invariants

- terminal jobs are never re-entered;
- retry attempt numbers only increase;
- a job belongs to exactly one project;
- milestone-specific jobs belong to exactly one milestone.

## Human-gate invariants

- a resolved gate cannot be responded to again;
- human approval must be explicit;
- a response must be associated with the correct project and gate;
- no missing response may be inferred.

---

# 36. Multiple Project Concurrency

Each project maintains its own independent state machine.

The global scheduler may therefore see:

```text
Project A
BUILDING / CODING

Project B
BUILDING / CI_RUNNING

Project C
WAITING_HUMAN / HUMAN_TEST

Project D
DESIGNING

Project E
PAUSED
```

These states are independent.

Only resource scheduling limits couple them.

A project waiting for external or human activity should not prevent another project from consuming an available Architect or Codex worker.

---

# 37. Status Query Consistency

Status queries must read committed persisted state.

They must not rely solely on:

- currently running processes;
- Telegram history;
- Architect memory;
- Codex output;
- stale cached GitHub information.

For fast-changing external information such as CI, Syntra may optionally perform reconciliation before replying.

A status response should distinguish:

```text
State
```

from:

```text
Activity
```

and from:

```text
Next action
```

Example:

> **FlowTrack**
>
> State: Building  
> Milestone: M4 — Diagnostic Capture Optimisation  
> Activity: GitHub Actions is running against PR #12  
> CI: 2 of 3 checks passed  
> Human action: None  
> Next: Architect review if CI succeeds

---

# 38. Initial State-Machine Scope

The first production version should implement the state machines defined here without adding unnecessary additional states.

New states should only be introduced when:

- behaviour differs materially;
- different transition policy is required;
- recovery semantics differ;
- user-visible status meaningfully differs.

Short-lived implementation details should normally be represented as:

- jobs;
- activities;
- events;
- metadata

rather than additional workflow states.

---

# 39. State Machine Completion Criteria

The state-machine design is complete when:

- every workflow stage in `WORKFLOW.md` maps to a formal state;
- every state has defined entry and exit paths;
- every privileged operation has a preceding guard;
- every retry loop has an explicit exit;
- every human dependency is represented by a durable gate;
- every external wait is recoverable;
- restart reconciliation is defined;
- multiple projects can maintain independent state;
- invalid transitions can be rejected deterministically;
- project status can be derived entirely from persisted state.

This state model should form the foundation of the Syntra Build orchestration engine.
