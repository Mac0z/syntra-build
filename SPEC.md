# Syntra Build — Specification

## 1. Document Status

**Status:** Draft for implementation approval  
**Intended repository:** GitHub  
**Runtime baseline:** Python 3.14 (`>=3.14,<3.15`)  
**Target host:** Syntra Raspberry Pi, Ubuntu Linux ARM64  
**Primary human interface:** Telegram  
**Persistent state:** SQLite  
**Required hosted CI:** GitHub Actions

This document is the implementation specification for Syntra Build itself. It converts the approved architecture, workflow, state-machine, interface, data-model, security, operations and ADR decisions into an executable development plan.

Once approved, this `SPEC.md` is the primary statement of **what must be built**. `AGENTS.md` will separately define the durable engineering rules for coding agents.

---

## 2. Source Precedence

Implementation must use the following precedence when source documents differ:

1. Explicit human decisions made after the design documents.
2. Accepted Architecture Decision Records.
3. This approved `SPEC.md`.
4. `SECURITY.md`, `STATE_MACHINE.md`, `INTERFACES.md`, `DATA_MODEL.md`, `ARCHITECTURE.md`, `WORKFLOW.md`, `OPERATIONS.md`, and `VISION.md`.
5. Provider-specific implementation assumptions.

The following superseding decisions are especially important:

- **Python 3.14** is the runtime baseline. Any older reference to Python 3.12 is superseded.
- **GitHub** is the canonical source-control platform for Syntra Build itself and generated projects. Any older Forgejo source-control assumption is superseded.
- **Architect approval is evidence, not a milestone state.** `ARCHITECT_APPROVED` may be recorded as a workflow event, review verdict or persisted piece of approval evidence, but it is not a member of the milestone-state vocabulary. The milestone remains within the approved state model and may progress toward `MERGE_READY` only after Syntra has validated all required evidence for the current revision.
- **Telegram transport does not own command routing or durable duplicate processing.** The Telegram adapter is responsible for bounded provider interaction, authorization, normalization, sending, and exposing stable provider identifiers and caller-managed update offsets. Deterministic command routing begins in M6. Durable consumed-update/duplicate state belongs to the trusted consuming workflow/persistence layer and must not be held authoritatively in the Telegram adapter.
- **Target-host validation begins after M6.** M6A is a development-deployment checkpoint on the Syntra Raspberry Pi, used to prove Python 3.14/ARM64 packaging, filesystem permissions, SQLite bootstrap and the real Telegram `ping` path before state-machine work continues. It is not the final production deployment model: managed `systemd` operation, operational health/metrics, hardened upgrades and self-hosting remain later milestones.

If a genuine unresolved conflict remains, implementation must stop at the affected scope and raise a human/Architect decision rather than silently choosing.

---

## 3. Product Definition

Syntra Build is an AI-assisted software-delivery orchestration system. It takes a software project from an initial human idea through design, specification, implementation, CI, review, human testing where required, merge and completion with minimal manual intervention.

Syntra Build is specifically an **application-development orchestration system**. It is not version-one general-purpose AI orchestration infrastructure.

The system coordinates four principal actors:

- **Human Project Owner** — defines intent, approves design, resolves subjective decisions and performs human tests when required.
- **Architect** — performs requirements analysis, architecture, specification, milestone task generation and implementation review.
- **Codex** — implements and tests code in a controlled local worktree.
- **Syntra Build** — owns authoritative workflow state, scheduling, privileged Git/GitHub actions, deterministic validation, recovery and status.

GitHub provides canonical source control, pull requests, Actions CI/CD and build artifacts.

---

## 4. Product Objectives

Syntra Build shall:

1. make software development controllable from an existing iPhone messaging application;
2. preserve all workflow position independently of AI conversational memory;
3. allow the Architect and human to design a project conversationally;
4. turn approved design into versioned `SPEC.md` and `AGENTS.md`;
5. provision GitHub repositories deterministically;
6. use one controlled worktree and normally one real PR per implementation milestone;
7. allow Codex to implement without giving it privileged GitHub credentials;
8. run required heavyweight/cross-platform CI through GitHub Actions;
9. require Architect review against the current exact PR revision;
10. support rework loops without creating replacement PRs unnecessarily;
11. represent human decisions and tests as durable gates;
12. merge only through deterministic Gatekeeper checks;
13. operate multiple independent projects within bounded Raspberry Pi resources;
14. survive process/host/network/external-provider failures through reconciliation;
15. provide accurate project status at any time through Telegram;
16. eventually use its own workflow to develop Syntra Build itself.

---

## 5. Version-One Non-Goals

Version one does not require:

- a custom mobile application;
- a general web project-management UI;
- enterprise multi-user RBAC or SSO;
- Kubernetes;
- Redis;
- RabbitMQ;
- Celery;
- PostgreSQL;
- distributed worker fleets;
- parallel active implementation milestones inside one project;
- self-hosted cross-platform CI runners;
- automatic production deployment of arbitrary generated applications;
- a generic plugin marketplace;
- billing or token-cost accounting;
- a high-availability control plane;
- repository deletion as part of ordinary project cancellation.

These features may be added later only through explicit design change.

---

## 6. Core Architectural Constraints

### 6.1 Control plane

Syntra Build alone owns authoritative project, milestone, job and human-gate state.

Architect, Codex, Telegram and GitHub produce inputs/events. They do not directly mutate authoritative workflow state.

### 6.2 Service model

Version one runs as a single long-running Python 3.14 service with clean internal module boundaries.

### 6.3 Persistence

SQLite is authoritative persisted workflow storage. WAL mode, foreign keys, migrations and transactional updates are mandatory.

### 6.4 Human interface

Telegram is the initial human interface and must be behind a replaceable messaging adapter.

### 6.5 AI providers

Architect and Codex must be accessed through adapters/contracts so provider-specific payloads do not enter the core domain.

### 6.6 Git/GitHub trust boundary

Codex produces filesystem changes. Syntra validates those changes and owns accepted commits, pushes, PR creation/update and merge.

Codex receives no privileged GitHub credentials.

### 6.7 CI

Required heavyweight/cross-platform CI runs in GitHub Actions and is bound to the exact PR head SHA.

### 6.8 Repository policy

Generated repositories are public by default. Private visibility requires an explicit approved design decision.

Syntra Build's own source is also stored in GitHub.

### 6.9 Recovery

Recovery is reconciliation, not blind replay.

### 6.10 Project isolation

A blocked, paused, waiting or failed project must not prevent unrelated projects from progressing.

---

## 7. Functional Requirements

### FR-001 — Messaging-first operation
The human shall be able to perform normal project creation, design, status and gate interactions through Telegram.

### FR-002 — Authorised messaging identity
Only configured authorised Telegram user/chat identities may query or mutate Syntra Build state.

### FR-003 — Project creation
The human shall be able to create a uniquely named project from Telegram. Repository creation shall not occur until design approval.

### FR-004 — Design conversation
Syntra shall route project design messages between the human and Architect and persist sufficient context to reconstruct the design conversation.

### FR-005 — Project documents
The Architect shall generate versioned `SPEC.md` and `AGENTS.md` drafts. Syntra shall preserve revisions and explicit approval state.

### FR-006 — Design approval
Implementation/provisioning shall require an explicit human design-approval gate.

### FR-007 — Repository visibility
Generated repositories shall default to public; private requires explicit approved intent.

### FR-008 — Repository provisioning
After design approval, Syntra shall create/configure the GitHub repository, commit the approved baseline and verify repository identity/visibility.

### FR-009 — Milestone sequencing
Syntra shall activate eligible milestones according to dependencies and shall run at most one active implementation milestone per project in version one.

### FR-010 — Architect task generation
For each implementation or rework cycle, Syntra shall ask the Architect for a structured Codex task based on the current approved documents, milestone and feedback.

### FR-011 — Controlled workspace
Syntra shall create/manage the branch and local worktree in which Codex operates.

### FR-012 — Codex execution
Syntra shall invoke Codex in the assigned workspace with bounded execution, captured result metadata and no privileged GitHub credentials.

### FR-013 — Deterministic change validation
Syntra shall inspect the actual worktree/diff and validate repository identity, branch, expected base, prohibited paths, secrets and policy before commit.

### FR-014 — Trusted commits and pushes
Only Syntra shall create accepted commits and push implementation branches.

### FR-015 — Real pull requests
Each milestone shall normally have one real GitHub PR, reused throughout rework.

### FR-016 — CI monitoring
Syntra shall monitor required GitHub Actions checks for the exact PR head SHA.

### FR-017 — CI rework
Implementation-related CI failure shall return to a bounded Codex rework loop; transient infrastructure failure shall use retry policy.

### FR-018 — Architect review
Architect review shall occur after required CI passes and shall be bound to the exact reviewed head SHA.

### FR-019 — Review rework
`CHANGES_REQUIRED` findings shall be persisted and returned to Codex through the same branch/PR cycle.

### FR-020 — Human decisions
When a product/technical/recovery decision is required, Syntra shall create a durable correlated human gate.

### FR-021 — Human testing
Human test requests shall identify the exact artifact/build/code revision being tested where applicable.

### FR-022 — Deterministic merge gate
Syntra shall merge only after deterministic checks prove all required CI, Architect and human gates are valid for the current revision.

### FR-023 — Merge verification
A milestone shall become complete only after GitHub merge state is independently verified.

### FR-024 — Project progression
After milestone completion, Syntra shall activate the next eligible milestone automatically until project completion.

### FR-025 — Pause/resume
The human shall be able to pause and resume an individual project without affecting unrelated projects.

### FR-026 — Cancellation
Cancellation shall stop future project work while preserving history and the GitHub repository by default.

### FR-027 — Status queries
The human shall be able to request current status for a project at any time without interrupting running work.

### FR-028 — Portfolio status
The human shall be able to request active projects and projects waiting for human action.

### FR-029 — Durable jobs/retries
Asynchronous work, attempts, retry delays and retry exhaustion shall be durable and queryable.

### FR-030 — Multi-project scheduling
The scheduler shall support multiple independent projects under configurable global worker limits and fairness policy.

### FR-031 — Restart recovery
On startup, Syntra shall reconcile all incomplete workflow/external operations before privileged scheduling resumes.

### FR-032 — Audit
Significant human, AI, Git, GitHub, CI, state-transition, security and merge operations shall be auditable.

### FR-033 — Health/metrics
Syntra shall expose health/readiness and Prometheus-compatible operational metrics.

### FR-034 — Backup/maintenance
Syntra shall support database-safe backups, integrity checking, recovery diagnostics and controlled upgrades.

### FR-035 — Self-hosting
The final bootstrap acceptance shall prove Syntra can safely manage a code change against its own GitHub repository.

---

## 8. Non-Functional Requirements

### NFR-001 — Durability
A process or host restart must not require human reconstruction of project workflow position.

### NFR-002 — Determinism
Privileged workflow decisions shall be made by validated code/policy, not solely by free-text AI output.

### NFR-003 — Idempotency
External side effects such as repository creation, PR creation and merge must be idempotent or reconciled before retry.

### NFR-004 — Isolation
Failure or waiting in one project must not block unrelated projects except where a deliberately global resource limit is exhausted.

### NFR-005 — Resource efficiency
The Pi is an orchestrator rather than a general build farm. External waits consume no scarce Codex slot.

### NFR-006 — Queryability
Current project status shall be readable efficiently from current-state tables without replaying complete event history.

### NFR-007 — Testability
Core domain/state/scheduler/gatekeeper logic must be testable offline without Telegram, GitHub, Architect or Codex live services.

### NFR-008 — Replaceability
Messaging, Architect, Codex and GitHub provider specifics shall live behind adapters/interfaces.

### NFR-009 — Observability
Structured logs, correlation IDs, state history, metrics and user-safe errors must permit diagnosis of a project lifecycle.

### NFR-010 — Security
Secrets and privileged credentials must not enter Codex/Architect contexts unless explicitly required by that provider's own isolated authentication boundary.

### NFR-011 — Compatibility
Syntra host runtime is Linux ARM64 on Python 3.14. Dependencies executed locally must support that platform.

### NFR-012 — Maintainability
Initial implementation shall remain a single service; internal boundaries must permit future extraction without requiring a rewrite of the domain state model.

---

## 9. Security Requirements

### SEC-001
The Syntra service and Codex execution must use separate unprivileged operating-system identities.

### SEC-002
Codex must not have access to privileged GitHub credentials, Telegram credentials, Syntra database credentials/data, or control-plane secrets.

### SEC-003
The Architect must not receive infrastructure credentials or direct host/GitHub administrative control.

### SEC-004
All Codex-produced changes must be secret-scanned before accepted commit/push.

### SEC-005
Git remote URLs must not embed credentials.

### SEC-006
Repository/worktree identity must be verified before trusted Git/GitHub mutations.

### SEC-007
Potential prompt injection in repository content, logs and external text must be treated as untrusted data.

### SEC-008
Human gate responses must be authenticated and correlated to the intended unresolved gate.

### SEC-009
A new PR head invalidates stale CI/Architect/human-test evidence as defined by policy.

### SEC-010
The running Syntra Build installation must never be directly modified by Codex, including when Syntra Build is itself the project under development.

### SEC-011
Cross-project workspace access is prohibited.

### SEC-012
HIGH/CRITICAL security conditions must block affected privileged actions and remain auditable.

---

## 10. Operational Requirements

### OPS-001
Run as a managed `systemd` service on the Syntra host.

### OPS-002
Startup order must validate config/database, run migrations/integrity checks and reconciliation before normal scheduling.

### OPS-003
Initial configurable concurrency defaults:
- Architect: 2
- Codex: 2
- repository provisioning: 1
- merge: 1

### OPS-004
Initial Codex timeout default: 60 minutes.

### OPS-005
Infrastructure retry attempts default to 4 with exponential backoff and jitter.

### OPS-006
Initial workflow-cycle defaults:
- Codex implementation attempts: 5
- CI rework cycles: 5
- Architect review cycles: 5

### OPS-007
Initial disk policy:
- below 20% free: warning;
- below 10% free: do not start new Codex work;
- below 5% free: critical system alert/unsafe scheduling state.

### OPS-008
Database backup occurs at least daily and before migration/upgrade.

### OPS-009
Waiting for human/CI/retry must not consume Codex capacity.

### OPS-010
System operational health (`STARTING`, `RECOVERING`, `HEALTHY`, `DEGRADED`, `DRAINING`, `UNHEALTHY`) is separate from project state.

---

## 11. State Model Requirements

The implementation shall conform to the approved state model.

### 11.1 Project states

`NEW`, `DESIGNING`, `DESIGN_APPROVAL`, `PROVISIONING`, `READY`, `BUILDING`, `WAITING_HUMAN`, `PAUSED`, `BLOCKED`, `COMPLETING`, `COMPLETE`, `FAILED`, `CANCELLED`.

### 11.2 Milestone states

`PENDING`, `READY`, `PREPARING_TASK`, `PREPARING_WORKSPACE`, `CODING`, `VALIDATING_CHANGES`, `COMMITTING`, `PUSHING`, `PR_CREATING`, `CI_RUNNING`, `CI_REWORK`, `ARCHITECT_REVIEW`, `REVIEW_REWORK`, `HUMAN_DECISION`, `HUMAN_TEST`, `MERGE_READY`, `MERGING`, `MERGE_VERIFY`, `COMPLETE`, `BLOCKED`, `FAILED`, `CANCELLED`.

Architect approval is a verdict/event and prerequisite for `MERGE_READY`; it is not a separate milestone state.

### 11.3 Job states

`QUEUED`, `DISPATCHED`, `RUNNING`, `WAITING_EXTERNAL`, `SUCCEEDED`, `RETRY_WAIT`, `FAILED`, `CANCELLED`, `ABANDONED`.

### 11.4 Human-gate states

`PENDING`, `NOTIFIED`, `RESPONDED`, `VALIDATED`, `RESOLVED`, `EXPIRED`, `CANCELLED`.

---

## 12. Required Persistent Domains

The final version-one schema shall represent, at minimum:

- projects;
- project documents and decisions;
- change requests;
- milestones and dependencies;
- jobs and attempts;
- human gates and responses;
- inbound/outbound messages and notifications;
- Architect requests/responses/reviews/findings;
- Codex runs/test reports;
- local repositories/workspaces;
- change sets and commits;
- GitHub repositories and pull requests;
- CI runs/checks;
- merge attempts;
- immutable workflow events;
- immutable state transitions;
- errors;
- recovery observations;
- system metadata/migration versions.

Exact physical schema may evolve during implementation provided it preserves the approved data-model semantics.

---

## 13. Required Interface Contracts

Version one shall implement validated domain contracts equivalent to:

- `InboundMessage`
- `OutboundMessage`
- `ProjectCommand`
- `HumanGateRequest`
- `HumanGateResponse`
- `ProjectStatusRequest`
- `ProjectStatusResponse`
- `ArchitectDesignRequest`
- `ArchitectDesignResponse`
- `SpecificationDraft`
- `ArchitectTaskRequest`
- `ArchitectTask`
- `ArchitectReviewRequest`
- `ArchitectReview`
- `ArchitectChangeAnalysisRequest`
- `ArchitectChangeAnalysis`
- `CodexRunRequest`
- `CodexRunResult`
- `WorkspaceRequest`
- `WorkspaceDescriptor`
- `ChangeSet`
- `CommitRequest`
- `CommitResult`
- `PushResult`
- `RepositoryProvisionRequest`
- `RepositoryDescriptor`
- `PullRequestCreateRequest`
- `PullRequestDescriptor`
- `CIStatus`
- `CIResult`
- `MergeRequest`
- `MergeResult`
- `WorkflowEvent`
- `JobDescriptor`
- `ErrorRecord`
- `RecoveryObservation`.

Durable structured contracts must be versioned and reject unknown state-changing enum semantics rather than silently reinterpret them.

---

## 14. Global Development Rules

Each implementation milestone shall:

1. normally map to one GitHub PR;
2. remain within the milestone scope;
3. include tests for new deterministic behaviour;
4. keep all existing tests passing;
5. update relevant documentation when a durable behaviour changes;
6. introduce no unexplained architecture deviation;
7. preserve backwards-compatible persisted state where already required or provide a migration;
8. be reviewed against this specification before merge.

A milestone may be split into smaller milestones if implementation proves broader than expected. Combining milestones into a larger PR should be avoided unless explicitly approved.

---

## 15. Milestone Plan

### M0 — Repository bootstrap

**Objective:** Create a minimal, testable Python 3.14 repository that establishes the engineering baseline without implementing orchestration behaviour.

**Dependencies:** None

**Required deliverables:**
- GitHub repository structure for Syntra Build
- Python package skeleton using a `src/` layout
- package metadata declaring `>=3.14,<3.15`
- test framework and first smoke test
- basic lint/format/static-analysis commands
- GitHub Actions workflow running the baseline checks on pull requests
- README with developer bootstrap commands
- placeholder `SPEC.md`, `AGENTS.md`, design documents and ADR directory committed as project sources of truth

**Acceptance criteria:**
- A clean checkout can create the development environment and run the complete baseline test command.
- CI runs on a pull request and passes on Python 3.14.
- Importing the top-level package succeeds.
- No application service, database, Telegram or external provider behaviour is implemented yet.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M1 — Configuration framework

**Objective:** Create typed configuration and deterministic path/bootstrap validation so later components do not read ad-hoc environment variables.

**Dependencies:** M0

**Required deliverables:**
- Configuration model grouped by filesystem, database, Telegram, Architect, Codex, GitHub, scheduler, retries, logging, metrics, backups and security thresholds
- environment/credential references separated from ordinary configuration
- default filesystem layout
- configuration validation with actionable errors
- startup configuration loader
- tests for valid, invalid and missing configuration

**Acceptance criteria:**
- Invalid configuration fails before any workflow action can run.
- Secrets are represented by references or dedicated secret inputs, not normal serialised configuration.
- Filesystem paths are canonicalised and checked against prohibited overlap.
- Configuration can be loaded in tests without contacting external services.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M2 — Structured logging and correlation

**Objective:** Establish structured, secret-safe diagnostics before asynchronous workflows are added.

**Dependencies:** M1

**Required deliverables:**
- structured application logging
- correlation ID generation and propagation helper
- standard log context fields for project, milestone, job, gate and component
- secret/redaction filter
- error normalisation primitives
- tests proving known secret patterns are not emitted

**Acceptance criteria:**
- Every log record can carry a correlation ID.
- Logging works before the database is available.
- Known credentials and Authorization-style values are redacted.
- A unit test demonstrates correlation context survives an async call boundary.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M3 — SQLite persistence bootstrap

**Objective:** Create the durable persistence foundation and migration mechanism.

**Dependencies:** M1, M2

**Required deliverables:**
- SQLite connection management
- WAL mode and foreign-key enforcement
- schema migration framework
- `system_metadata` and migration metadata
- transaction helper
- database integrity check
- test database fixtures
- database-safe backup primitive

**Acceptance criteria:**
- A fresh database migrates from zero to current schema automatically.
- Re-running migrations is idempotent.
- Foreign-key enforcement is verified by test.
- WAL mode is verified.
- A backup created from a live test database restores and passes integrity check.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M4 — Core domain records

**Objective:** Persist the minimum domain entities required to support projects and early Telegram testing.

**Dependencies:** M3

**Required deliverables:**
- project record
- project document record
- project decision record
- milestone record
- job record
- human gate record
- workflow event record
- state transition record
- error record
- repositories/adapters for CRUD and indexed current-state reads

**Acceptance criteria:**
- Projects can be created, read and listed from SQLite.
- Canonical project names are unique.
- Core enums are validated.
- Current-state reads do not require event replay.
- Foreign-key relationships enforce project/milestone ownership.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M5 — Telegram gateway

**Objective:** Bring up the real Telegram transport early so later application workflows can be exercised through the approved human interface without coupling core logic to Telegram provider payloads.

**Dependencies:** M1, M2, M3, M4

**Required deliverables:**
- Telegram adapter using long polling unless a later deployment decision changes it
- authorised numeric Telegram user allowlist
- inbound text-message normalisation
- outbound plain-text message sending
- bounded polling with caller-supplied update offset
- stable Telegram update and message identifiers exposed for later durable duplicate handling
- timezone-aware UTC conversion of Telegram timestamps
- typed transport, protocol and provider errors
- Telegram token loaded only through protected secret handling
- safe structured logging that excludes credentials, credential-bearing URLs, unrestricted provider payloads and message bodies

**Transport ownership rules:**
- one adapter call performs one bounded Telegram interaction;
- the adapter must not contain an infinite polling/scheduler loop;
- the adapter must not contain hidden durable retry policy;
- the adapter must not persist update offsets;
- the adapter must not maintain authoritative in-memory duplicate state;
- supplying the same offset may therefore return the same provider update again;
- the trusted consumer is responsible for persisting consumed-update progress and ensuring an update is applied to workflow state at most once.

**Acceptance criteria:**
- An authorised Telegram user can produce a correctly normalised inbound text message.
- An unauthorised Telegram user is not treated as an authorised instruction source.
- Telegram long polling uses the configured bounded timeout.
- A caller can supply an update offset and it is passed to Telegram correctly.
- Multiple provider updates retain deterministic provider order.
- Unsupported or non-text update forms are ignored or rejected according to the adapter contract without crashing the service.
- Outbound plain-text messages can be sent with the required chat, thread and reply identifiers.
- Telegram timestamps are normalised to timezone-aware UTC values.
- Malformed provider responses and Telegram API failures produce typed, safe errors.
- Network failures preserve useful exception causality without exposing the Telegram token.
- Telegram token, credential-bearing URLs and unrestricted message contents are absent from logs.
- Repeating a poll with the same caller-managed offset does not rely on hidden adapter state to suppress a duplicate.
- All tests are offline, deterministic and credential-free.

**Human acceptance:** None beyond PR review. Command handling and application-level behaviour are introduced by later milestones.

**Exit gate:** Transport-level acceptance criteria pass in CI and the PR is approved against this specification.

### M6 — Basic command and intent routing

**Objective:** Introduce a deterministic application-level command-routing layer behind the Telegram transport before AI interpretation is used.

**Dependencies:** M5

**Required deliverables:**
- normalised command model independent of Telegram provider payloads
- deterministic parsing/routing for `ping`, `health`, `projects`, `status <project>`, `pause <project>`, `resume <project>` and `cancel <project>`
- optional leading `/` accepted for Telegram-style commands where appropriate
- unsupported or malformed commands return deterministic help
- exact project resolution by stable internal ID or approved canonical project name
- read-only and state-changing command distinction
- audit event for state-changing command requests
- clear separation between command parsing, command routing and Telegram transport
- command handlers expressed through application-facing services/contracts rather than Telegram-specific logic
- extensible intent-router interface for later natural-language interpretation
- no AI interpretation for commands that can be resolved deterministically

**`ping` semantics:**
- `ping` verifies that the messaging-to-routing path is responsive;
- it returns a deterministic response;
- it does not imply that every external dependency is healthy.

**`health` semantics:**
- `health` reports the current local Syntra service-health view available at this milestone;
- it must not fabricate health for components that have not yet been implemented;
- richer operational health/readiness remains part of later operational milestones.

**Duplicate-processing ownership:**
- M6 must not implement authoritative duplicate handling inside the Telegram adapter;
- commands must retain the source Telegram `update_id`/message identity or an equivalent stable source reference so the later persistence/workflow consumer can record consumption atomically;
- once durable message/event persistence is introduced, successful processing and consumed-update progression must be owned by trusted Syntra state rather than process memory.

**Acceptance criteria:**
- `ping` returns the deterministic routing response for an authorised inbound command.
- `health` returns the deterministic health information actually available at this stage.
- supported commands are parsed independently of Telegram raw JSON.
- Telegram commands reach the correct application service without Telegram-specific types entering the domain layer.
- `projects` lists persisted projects once the required project-read service exists.
- Unknown or ambiguous project names do not mutate state.
- Unsupported commands return deterministic help.
- State-changing commands are authorised and persisted before execution.
- unauthorised Telegram input never reaches command execution.
- command routing remains offline-testable without the real Telegram API.
- no project state transition is performed merely by parsing a command; state mutation occurs through the appropriate application service boundary.
- no AI interpretation is used for the deterministic command set.

**Human acceptance:** None beyond PR review unless an exposed routing behaviour cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M6A — Syntra host development deployment

**Objective:** Deploy the merged M0-M6 stack to the target Syntra Raspberry Pi and prove the real host/runtime/Telegram path before introducing authoritative project state-machine behaviour.

**Dependencies:** M6

**Required deliverables:**
- documented manual deployment from an exact GitHub `main` commit SHA to the target host
- Python 3.14 virtual environment and package installation on Linux ARM64
- dedicated unprivileged Syntra runtime identity consistent with the security model
- development runtime layout using `/opt/syntra-build`, `/etc/syntra-build`, `/var/lib/syntra-build` and `/var/log/syntra-build`
- protected local configuration/secrets with the Telegram bot token kept outside Git and ordinary serialised configuration
- SQLite bootstrap using the existing migration/integrity path against the target data directory
- deterministic local smoke command proving configuration, logging, database and M6 routing can initialise on the host
- bounded Telegram smoke runner that performs one polling interaction, routes an authorised M6 command and sends the resulting response through M5
- deployment/runbook instructions covering install/update, exact revision verification, smoke execution and safe failure reporting
- tests for any new deterministic deployment/smoke wiring that can run in CI without a real host or Telegram credential

**Development-deployment boundaries:**
- M6A is a host-validation checkpoint, not the final production service lifecycle;
- deployment is manual and operator-driven at this stage; GitHub Actions must not SSH to or automatically deploy the Syntra host;
- the host checkout/install must be tied to an explicit Git commit SHA so the tested revision is known;
- the smoke runner performs bounded interactions only and must not introduce an infinite Telegram polling loop or scheduler;
- no `systemd` service is required by M6A; managed long-running `systemd` operation remains an operational milestone requirement;
- M6A must not implement M7 project transitions, later state machines, durable Telegram-offset processing, workflow-event dispatch, scheduler behavior, Architect/Codex/GitHub orchestration or self-hosting;
- real credentials remain host-local and must never be committed, printed or included in CI artifacts/logs.

**Acceptance criteria:**
- The exact approved `main` revision can be installed on the Syntra Raspberry Pi using Python 3.14 on Linux ARM64.
- The runtime directories exist with ownership/permissions that allow Syntra to run unprivileged without making secrets broadly readable.
- Production-like configuration loads successfully from the host layout while the Telegram token remains outside ordinary serialised configuration.
- A fresh SQLite database can be bootstrapped under `/var/lib/syntra-build`, uses the existing migration path, reports WAL mode and passes the existing integrity check.
- A local smoke invocation initialises configuration/logging/database/routing and verifies deterministic `ping` routing returns `pong` without contacting Telegram.
- A bounded real Telegram smoke test allows an authorised user to send `/ping` and receive `pong` through the M5 transport and M6 router.
- `/health` and unsupported-command help can be exercised through the same bounded smoke path without inventing future health semantics.
- An unauthorised Telegram sender is not routed as an instruction source.
- Telegram credentials, credential-bearing URLs and unrestricted message bodies are absent from repository content, command output and application logs.
- Re-running the documented installation/bootstrap process against the same revision is safe and does not corrupt the database or runtime layout.
- All CI-testable behavior remains deterministic and credential-free, and all M0-M6 tests continue to pass.

**Human acceptance:** On the real Syntra host, confirm from the authorised Telegram account that `/ping` returns `pong`, `/health` returns the truthful current local health response, and an unsupported command returns deterministic help. Confirm the deployed revision matches the approved GitHub `main` SHA.

**Exit gate:** CI passes for the implementation PR, the PR is approved and merged, and the documented target-host smoke test succeeds against that merged revision.

### M7 — Project state machine

**Objective:** Implement the authoritative project lifecycle and guards.

**Dependencies:** M4, M6A

**Required deliverables:**
- project states from `STATE_MACHINE.md`
- transition table/guard service
- transactional current-state update plus immutable transition history
- `resume_state` handling for pause
- illegal-transition rejection
- project activity field separate from state

**Acceptance criteria:**
- All documented valid project transitions are covered by tests.
- Representative illegal transitions are rejected.
- A transition writes current state and history atomically.
- Paused projects retain a valid resume target.
- Project state never changes directly from an Architect/Codex/external payload.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M8 — Milestone state machine

**Objective:** Implement the full milestone lifecycle independently of external integrations.

**Dependencies:** M7

**Required deliverables:**
- milestone state enum and transition rules
- milestone guards
- resume/recovery target metadata
- activation/completion rules
- one active implementation milestone per project
- transition history

**Acceptance criteria:**
- The happy-path milestone sequence can be simulated entirely offline.
- CI, review, rework, human-test and merge branches are represented.
- A complete milestone cannot exist without required completion evidence once those references are populated.
- Only one implementation milestone per project may be active in version one.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M9 — Job state machine

**Objective:** Implement durable asynchronous work tracking separately from business workflow state.

**Dependencies:** M4, M7

**Required deliverables:**
- job states
- job attempts
- worker classes
- attempt counters
- retry metadata
- terminal-state protection
- abandoned-job representation

**Acceptance criteria:**
- Job retries preserve prior attempts.
- Terminal attempts are immutable.
- `WAITING_EXTERNAL` does not imply a local worker is occupied.
- Lost work can be represented as `ABANDONED` without falsely marking success.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M10 — Human gate state machine

**Objective:** Implement durable human approvals, decisions and tests, then expose them through Telegram.

**Dependencies:** M5, M7, M8

**Required deliverables:**
- human gate types and states
- gate creation service
- gate notification through Telegram
- response correlation by gate ID/project/milestone
- design approval, decision and test response schemas
- resume targets
- outstanding-actions Telegram command

**Acceptance criteria:**
- A Telegram response can resolve only the intended unresolved gate.
- A resolved gate cannot be answered again.
- Human silence never resolves a gate.
- `waiting`/equivalent command lists outstanding human actions.
- A project in `WAITING_HUMAN` has at least one unresolved gate.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M11 — Workflow event framework

**Objective:** Introduce immutable domain events and deduplicated event processing.

**Dependencies:** M7, M8, M9, M10

**Required deliverables:**
- workflow event envelope
- event persistence
- correlation/causation identifiers
- processing status
- deduplication key support
- event dispatcher
- audit projection into transition history where appropriate

**Acceptance criteria:**
- Duplicate external events do not cause duplicate workflow effects.
- Events remain immutable after persistence.
- Failed event processing is visible and retryable according to policy.
- Project/milestone/job/gate identity is preserved through event dispatch.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M12 — Scheduler core

**Objective:** Schedule eligible durable jobs across projects while respecting worker-class capacity.

**Dependencies:** M9, M11

**Required deliverables:**
- job eligibility query
- worker-class semaphores/limits
- dispatch loop
- project-local pause/cancel eligibility checks
- system drain mode
- worker slot release on external wait
- configurable concurrency defaults

**Acceptance criteria:**
- Configured limits are never exceeded.
- A paused/cancelled project receives no new implementation work.
- CI/human waiting consumes no Codex slot.
- Scheduler can restart from persisted queued jobs.
- Status/Telegram responsiveness remains available while workers are active.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M13 — Retry, backoff and fairness

**Objective:** Add bounded failure handling and prevent one project from monopolising workers.

**Dependencies:** M12

**Required deliverables:**
- transient retry classification
- exponential backoff with jitter
- per-job max attempts
- milestone rework counters
- round-robin or equivalent fair project scheduling
- retry-exhaustion transition to `BLOCKED`/`FAILED` according to policy
- Telegram notification on actionable exhaustion

**Acceptance criteria:**
- Transient failures retry at scheduled times.
- One looping project cannot reacquire all Codex capacity indefinitely.
- Retry exhaustion is persisted and visible through status.
- Infrastructure retries and workflow rework cycles are counted separately.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M14 — Project creation flow

**Objective:** Allow a real project to be created through Telegram and enter the design lifecycle.

**Dependencies:** M6, M7, M11

**Required deliverables:**
- `CREATE_PROJECT` intent
- name validation
- canonical-name conflict checks
- owner/messaging context persistence
- initial request persistence
- project `NEW → DESIGNING` transition
- Telegram confirmation and status

**Acceptance criteria:**
- An authorised Telegram user can create a uniquely named project.
- No GitHub repository is created at this stage.
- Duplicate/conflicting names are rejected safely.
- The project is immediately queryable from Telegram.
- Creation survives service restart.

**Human acceptance:** Create a disposable test project through Telegram and confirm its visible state/identity are correct.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M15 — Design conversation persistence

**Objective:** Persist enough human/Architect design context to reconstruct an Architect session without relying on provider memory.

**Dependencies:** M14

**Required deliverables:**
- inbound/outbound design-message records
- project decisions and assumptions
- open/resolved question representation
- conversation reconstruction service
- document-draft storage
- repository visibility decision defaulting to public unless explicit private choice is recorded

**Acceptance criteria:**
- A multi-turn design conversation can be reconstructed from local persistence.
- Provider session IDs are optional optimisation only.
- Repository visibility is deterministically `public` when no explicit private decision exists.
- A service restart does not lose the design conversation.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M16 — Architect adapter

**Objective:** Integrate the initial Architect provider through structured, replaceable contracts.

**Dependencies:** M13, M15

**Required deliverables:**
- Architect provider interface
- design-turn request/response
- structured output validation
- timeouts/retry integration
- provider request/response persistence
- model/config metadata
- malformed-output rejection
- Telegram relay of Architect questions/responses

**Acceptance criteria:**
- Architect provider-specific payloads do not enter the domain layer.
- A design response can be `ASK_USER`, `PROPOSE_DESIGN`, `READY_TO_DRAFT` or `BLOCKED` according to contract.
- Malformed structured responses do not advance project state.
- Provider failure is retryable without losing design history.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M17 — SPEC and AGENTS generation and design approval

**Objective:** Complete the design phase with versioned source-of-truth documents and explicit human approval.

**Dependencies:** M10, M16

**Required deliverables:**
- Architect generation of `SPEC.md` and `AGENTS.md` drafts
- document revisioning and hashes
- design summary
- design-approval human gate
- REQUEST_CHANGES loop
- approved document baseline
- approved repository visibility persistence

**Acceptance criteria:**
- Design cannot enter provisioning without explicit human approval.
- REQUEST_CHANGES creates new document revisions without rewriting history.
- Exactly one current approved revision per document type exists.
- Telegram shows the proposed repository visibility before approval.
- Approved document hashes are available for repository-baseline verification.

**Human acceptance:** Review and approve a generated design package through Telegram, including repository visibility.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M18 — GitHub repository provisioning

**Objective:** Provision the approved project repository deterministically in GitHub.

**Dependencies:** M17

**Required deliverables:**
- GitHub adapter repository create/get/configure operations
- GitHub authentication isolated to trusted adapter
- public-default/private-explicit visibility rule
- lookup-before-create/idempotency
- initial local repository creation
- commit of approved `SPEC.md`, `AGENTS.md` and baseline project files
- push to `main`
- repository identity/visibility verification

**Acceptance criteria:**
- Provisioning cannot start before design approval.
- A public project is created public; an explicitly approved private project is created private.
- Repeating a partially completed provisioning job does not create a duplicate repository.
- Committed design documents match approved hashes.
- Repository identity is persisted and verified before project becomes `READY`.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M19 — Workspace and trusted Git management

**Objective:** Create controlled milestone branches/worktrees and trusted Git operations owned by Syntra.

**Dependencies:** M18

**Required deliverables:**
- managed local clone/bare repository
- fetch/synchronisation
- branch naming policy
- worktree creation/removal
- repository/remote/base-SHA verification
- change inspection
- trusted staging/commit/push primitives
- commit identity and audit records

**Acceptance criteria:**
- Only configured project paths are used.
- A milestone workspace is bound to expected project/repository/branch/base SHA.
- Codex is not responsible for accepted commits or pushes.
- Git remote URLs contain no embedded credentials.
- Workspace recovery can identify dirty/orphaned worktrees.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M20 — Codex runner

**Objective:** Run Codex as an unprivileged coding worker in the assigned workspace.

**Dependencies:** M12, M19

**Required deliverables:**
- Codex provider/runner interface
- dedicated `syntra-codex` execution identity
- controlled environment
- task input contract
- process tracking
- stdout/stderr artifact capture
- timeout and process-tree termination
- result normalisation
- no privileged GitHub/Telegram/control-plane credentials

**Acceptance criteria:**
- Codex can modify only the assigned worktree under normal policy.
- Codex receives no privileged GitHub credential.
- Timeout produces `TIMED_OUT` and terminates child processes.
- A successful Codex process result does not automatically cause commit or milestone success.
- The filesystem diff, not Codex's self-report, remains authoritative.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M21 — Change validation and security scanning

**Objective:** Validate Codex output before trusted Git accepts it.

**Dependencies:** M20

**Required deliverables:**
- diff capture and hash
- expected branch/remote/base validation
- non-empty-change validation
- workspace escape/path traversal checks
- protected-path policy
- secret scanning
- unexpected Git-history detection
- validation findings and classification
- rework/block decision mapping

**Acceptance criteria:**
- A changed diff after validation cannot be committed under the earlier validation result.
- A likely real credential blocks commit/push and is redacted in logs.
- Unexpected remote/branch/history changes block acceptance.
- Protected workflow changes require explicit milestone scope.
- Fixable validation failures can be returned to Codex without creating a PR.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M22 — Pull request lifecycle

**Objective:** Create and maintain one real implementation PR per milestone.

**Dependencies:** M21

**Required deliverables:**
- commit after successful change validation
- trusted branch push
- PR create-or-get
- PR descriptor persistence
- head SHA tracking
- same-PR update on rework
- PR identity reconciliation

**Acceptance criteria:**
- A milestone has at most one active implementation PR.
- Retrying PR creation adopts an existing matching PR rather than duplicating it.
- PR head SHA is updated after every Syntra-managed rework push.
- The project/milestone/repository/branch identity is verified before PR operations.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M23 — GitHub Actions CI monitoring

**Objective:** Track required GitHub CI checks for the exact PR head without consuming scarce local worker capacity.

**Dependencies:** M22

**Required deliverables:**
- CI monitor
- required-check discovery/policy
- CI run/check persistence
- head-SHA binding
- adaptive polling
- pass/fail/cancel/unknown handling
- failure classification
- implementation-failure rework event
- transient-infrastructure retry path

**Acceptance criteria:**
- Architect review cannot start until required CI passes for the current head SHA.
- Pushing a new commit makes prior CI stale for merge purposes.
- CI wait consumes no Codex slot.
- Implementation failures enter CI rework; transient failures follow infrastructure retry policy.
- Telegram status can report individual required-check progress.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M24 — Architect review and rework

**Objective:** Have the Architect review the exact passing PR revision against the approved specification.

**Dependencies:** M16, M23

**Required deliverables:**
- Architect review request containing milestone, SPEC/AGENTS revision, PR diff, CI result and current head SHA
- structured review verdict
- finding persistence
- finding severity/requirement references
- reviewed-SHA binding
- CHANGES_REQUIRED rework task generation
- same-PR Codex rework loop
- stale approval invalidation

**Acceptance criteria:**
- `APPROVE` is valid only for the reviewed current head SHA.
- `CHANGES_REQUIRED` includes actionable findings and returns through Codex/validation/CI before another review.
- A new commit invalidates earlier approval for merge.
- Review findings remain historically auditable.
- The Architect cannot merge or mutate GitHub directly.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M25 — Human decision and human-test execution

**Objective:** Complete milestone-specific human intervention flows, including build/SHA binding.

**Dependencies:** M10, M23, M24

**Required deliverables:**
- Architect-triggered product/technical decision gates
- human-test gate creation
- test instructions
- artifact/build reference
- tested head-SHA/CI-run binding
- Telegram PASS/FAIL/BLOCKED handling
- FAIL → rework flow
- decision resume targets

**Acceptance criteria:**
- PASS is accepted only for the intended test gate/artifact/code revision.
- FAIL returns the milestone to rework with human evidence available to the Architect.
- A new code revision invalidates an obsolete human-test result where policy requires retest.
- Other projects continue while one project waits for human action.

**Human acceptance:** Exercise at least one real human-decision or human-test gate through Telegram.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M26 — Deterministic Gatekeeper and merge

**Objective:** Implement the final privileged merge policy and verified milestone completion.

**Dependencies:** M24, M25

**Required deliverables:**
- merge-eligibility request/result
- checks for expected repository/project/milestone/PR/branch
- current head-SHA verification
- required CI pass verification
- current Architect APPROVE verification
- unresolved finding/gate checks
- pause/cancel/security-block checks
- trusted merge call
- merge reconciliation/verification
- milestone COMPLETE transition

**Acceptance criteria:**
- No AI output alone can bypass Gatekeeper checks.
- Merge is rejected if CI/review/test evidence applies to a stale SHA.
- Paused/cancelled/blocked projects cannot merge.
- A successful GitHub merge is independently verified before milestone becomes `COMPLETE`.
- Merge attempts are audited and initially serialised globally.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M27 — Recovery and reconciliation

**Objective:** Make restart recovery a first-class path for every state with an uncertain external side effect.

**Dependencies:** M26

**Required deliverables:**
- startup `RECOVERING` mode
- incomplete project/job discovery
- PR creation reconciliation
- CI reconciliation
- merge reconciliation
- Codex process/worktree reconciliation
- human-gate restoration
- branch/push reconciliation
- unknown-job abandonment
- safe replacement jobs
- recovery observations/audit

**Acceptance criteria:**
- Privileged scheduling does not start until reconciliation completes.
- Restart during `PR_CREATING`, `CI_RUNNING`, `MERGING`, `CODING` and `HUMAN_TEST` is covered by automated/integration tests.
- Recovery never blindly repeats a merge or PR create.
- Ambiguous unsafe state blocks only the affected project when possible.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M28 — Full status service and natural-language status projection

**Objective:** Turn persisted state into useful Telegram operational answers without disturbing work.

**Dependencies:** M27

**Required deliverables:**
- single-project status projection
- active-project list
- waiting-for-me list
- current state vs current activity distinction
- worker/Codex activity
- branch/PR/head SHA
- CI summary
- Architect verdict
- human actions
- retry/latest-error/next-action
- optional lightweight GitHub reconciliation before response
- natural-language project-status intent mapping

**Acceptance criteria:**
- Status is derived primarily from committed Syntra state, not AI conversation memory.
- A status query never cancels or interrupts running work.
- `What projects are active?` and `Is anything waiting for me?` work through Telegram.
- Status remains useful for PAUSED, BLOCKED, FAILED and WAITING_HUMAN projects.

**Human acceptance:** Confirm the Telegram status responses are clear and useful for active, waiting and failed/blocked states.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M29 — Operational health and Prometheus metrics

**Objective:** Expose system health and resource/workflow metrics compatible with the existing Syntra monitoring stack.

**Dependencies:** M12, M27, M28

**Required deliverables:**
- system health states
- `/health`, `/ready`, `/metrics` endpoints
- Prometheus counters/gauges/histograms for projects, jobs, Codex, Architect, CI, API failures, state transitions, disk/artifact usage
- scheduler resource guard hooks
- basic Grafana dashboard definition or documented panel queries
- degraded/unhealthy behaviour

**Acceptance criteria:**
- `/ready` is false during startup recovery and unsafe system-wide conditions.
- Metrics expose no secrets or sensitive project content.
- Disk/resource thresholds can prevent new Codex dispatch while leaving status/messaging available.
- Existing Prometheus can scrape Syntra Build metrics.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M30 — Backup, maintenance and administrative CLI

**Objective:** Make routine operation and recovery possible without direct database manipulation.

**Dependencies:** M27, M29

**Required deliverables:**
- automatic SQLite backup
- backup retention hooks
- pre-migration/pre-upgrade backup
- restore verification command
- database integrity command
- administrative CLI commands for health/status/projects/reconcile/backup/version
- graceful drain/shutdown support
- systemd unit/install documentation

**Acceptance criteria:**
- A backup can be restored into a clean temporary location and pass integrity checks.
- Administrative reconciliation reports observations before unsafe mutation.
- The CLI cannot fake CI, Architect approval or milestone completion.
- The service can stop/start under systemd and recover persisted project state.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M31 — Security hardening and negative-path test suite

**Objective:** Apply and verify the full `SECURITY.md` threat model before end-to-end autonomous use.

**Dependencies:** M20, M26, M29, M30

**Required deliverables:**
- filesystem permission policy
- dedicated service identities
- credential exposure tests
- cross-project isolation tests
- prompt-injection resilience tests at trust boundaries
- secret-scanning negative tests
- protected-path negative tests
- unauthorised Telegram tests
- repository identity mismatch tests
- stale-SHA rejection tests
- resource exhaustion limits
- security event/severity persistence

**Acceptance criteria:**
- Codex cannot read control-plane credentials or database under the deployed permission model.
- A project cannot access another project's workspace under normal execution policy.
- Prompt-injected Architect/Codex content cannot directly perform privileged GitHub actions.
- HIGH/CRITICAL security conditions block affected privileged actions.
- All security invariants defined by the design are represented by automated tests where technically testable.

**Human acceptance:** None beyond PR review unless the milestone exposes behaviour that cannot be validated automatically.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M32 — End-to-end generated-project acceptance

**Objective:** Demonstrate the complete production workflow by building a small real application from Telegram with no manual Git/GitHub intervention.

**Dependencies:** M31

**Required deliverables:**
- test project initiated from Telegram
- real design conversation
- generated/approved SPEC and AGENTS
- GitHub repository provisioning
- at least two implementation milestones
- Codex implementation
- real PRs
- GitHub Actions
- at least one controlled rework path
- Architect approval
- human gate if applicable
- automatic gated merge
- project completion/status history
- restart test during active workflow

**Acceptance criteria:**
- The complete project starts from Telegram and finishes in GitHub without the human manually running Git or GitHub commands.
- At least one CI or Architect rework loop is demonstrated or deliberately injected.
- A Syntra restart during active work recovers correctly.
- The user can query progress throughout.
- All milestones end with auditable PR/CI/review/merge evidence.

**Human acceptance:** Act as project owner for the full generated-project acceptance demonstration.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

### M33 — Syntra self-hosting acceptance

**Objective:** Prove the bootstrap is complete by using Syntra Build to make a controlled change to Syntra Build itself.

**Dependencies:** M32

**Required deliverables:**
- register/use the Syntra Build GitHub repository as an eligible project
- create an isolated worktree separate from the running installation
- Architect task for a small real Syntra Build change
- Codex implementation without privileged GitHub credentials
- Syntra validation/commit/push/PR
- GitHub Actions
- Architect review
- Gatekeeper merge
- controlled deployment remains separate from coding worktree

**Acceptance criteria:**
- The running service is never modified directly by Codex.
- Syntra Build successfully manages a PR against its own GitHub repository.
- All ordinary SHA/CI/review/security gates apply to the self-hosted change.
- The self-change is merged through the same controlled workflow as a generated project.
- Completion of this milestone constitutes bootstrap/self-hosting acceptance.

**Human acceptance:** Approve the self-hosting test change and confirm the running Syntra installation was not directly modified by the coding worktree.

**Exit gate:** All listed acceptance criteria pass in CI and the PR is approved against this specification.

---

## 16. Cross-Milestone Integration Tests

In addition to per-milestone tests, the repository shall accumulate integration tests for the following critical chains:

### IT-001 — State persistence
Create project → transition state → restart service/application fixture → verify identical authoritative current state/history.

### IT-002 — Telegram authorisation
Authorised message succeeds; unauthorised message cannot query or mutate.

### IT-003 — Human gate
Create gate → notify → correlate response → validate → resolve → reject duplicate response.

### IT-004 — Scheduler fairness
Multiple eligible projects with bounded workers progress without one looping project starving the rest.

### IT-005 — PR idempotency
Simulate timeout after PR creation → reconcile → adopt same PR without creating another.

### IT-006 — CI SHA freshness
Pass CI for SHA A → push SHA B → verify SHA A pass is unusable for review/merge.

### IT-007 — Architect SHA freshness
Approve SHA A → push SHA B → verify approval is stale.

### IT-008 — Human-test freshness
Pass test for artifact/SHA A → supersede code → verify policy requires current evidence before merge.

### IT-009 — Merge recovery
Simulate unknown result during merge → reconcile GitHub → never duplicate merge.

### IT-010 — Codex loss
Lose Codex process mid-run → mark attempt abandoned → preserve worktree → do not silently commit partial changes.

### IT-011 — Secret detection
Introduce synthetic credential fixture → validation blocks accepted commit/push and logs do not expose full fixture secret.

### IT-012 — Cross-project isolation
Project A job cannot resolve or mutate Project B resources through project-name ambiguity.

---

## 17. CI Requirements for Syntra Build Repository

Every implementation PR for Syntra Build shall run GitHub Actions checks including at minimum:

- Python 3.14 environment/setup;
- package install;
- unit test suite;
- integration tests that do not require live external credentials;
- configured lint/format/static checks;
- migration/schema validation where applicable.

Live Telegram/GitHub/Architect/Codex integration tests shall be separated from ordinary credential-free PR tests and must not expose secrets to arbitrary PR code.

The exact workflow matrix may evolve, but Python 3.14 Linux execution is mandatory.

---

## 18. Definition of Milestone Done

A milestone is done only when:

1. all required deliverables exist;
2. all milestone acceptance criteria pass;
3. required automated tests are present and passing;
4. required human acceptance is complete;
5. the implementation remains within architecture/security boundaries;
6. required CI is green for the current PR head;
7. Architect review approves the current PR head;
8. no unresolved required findings/gates remain;
9. the PR is merged and merge verified;
10. relevant documentation is updated.

For the manual bootstrap development of Syntra Build before Syntra can orchestrate itself, the human/ChatGPT/Codex process used for FlowTrack may perform the orchestration steps manually while preserving the same one-milestone/one-PR discipline.

---

## 19. Definition of Project Complete

Syntra Build version one is complete only when:

- M0 through M33 are complete;
- all global functional, non-functional, security and operational requirements are satisfied;
- all cross-milestone critical integration tests pass;
- the production service runs under `systemd` on the Syntra host;
- Telegram can create/design/control/query projects;
- a real generated project has completed the full autonomous workflow;
- restart/recovery has been demonstrated during active work;
- backups and restore verification work;
- Prometheus operational metrics are available;
- security boundaries have been tested;
- Syntra Build has successfully managed a controlled pull request against **its own GitHub repository** without Codex directly modifying the running installation.

Completion of M33 is the bootstrap threshold: after it, Syntra Build is capable of participating in its own future development workflow.

---

## 20. Approval

Approval of this document means:

- the requirements and milestone structure are accepted as the implementation baseline;
- implementation may proceed to repository bootstrap/M0;
- scope changes after approval should be recorded as explicit specification revisions rather than silently incorporated;
