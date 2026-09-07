# Syntra Build — Architecture

## 1. Purpose

This document defines the high-level technical architecture for Syntra Build.

It describes the major components, their responsibilities, boundaries, data flows, trust relationships, deployment model, and interaction patterns required to implement the behaviour defined in:

- `VISION.md`
- `WORKFLOW.md`
- `STATE_MACHINE.md`

This document is intentionally architectural rather than code-level.

Detailed schemas, API contracts, database tables, configuration keys, and implementation classes will be defined in later design documents and ultimately in `SPEC.md`.

---

# 2. Architectural Goals

The architecture must support the following primary goals:

- conversational project creation through an existing messaging platform;
- AI-assisted design and requirements analysis;
- generation and approval of `SPEC.md` and `AGENTS.md`;
- automatic GitHub repository creation;
- autonomous milestone execution;
- Codex-based implementation;
- real GitHub pull requests;
- GitHub Actions CI/CD;
- Architect review and rework loops;
- explicit human decision and test gates;
- safe automated merging;
- multi-project operation;
- durable recovery after restart or failure;
- natural-language project status queries;
- execution within the resource limits of the Syntra Raspberry Pi host.

---

# 3. Architectural Principles

## 3.1 Syntra is the control plane

Syntra Build owns:

- workflow state;
- scheduling;
- project identity;
- milestone identity;
- privileged Git operations;
- privileged GitHub operations;
- human gates;
- retry policy;
- recovery decisions;
- merge authority.

The Architect and Codex are workers.

They do not own orchestration state.

---

## 3.2 AI components are replaceable

The architecture must not couple the orchestration engine directly to a specific AI model.

Syntra interacts with AI capabilities through adapters.

Initial adapters will include:

- Architect Provider;
- Codex Provider.

Future providers may replace either without changing the core state machine.

---

## 3.3 Privileged actions remain deterministic

AI may recommend actions, but deterministic Syntra code decides whether privileged operations are allowed.

Examples:

- repository creation;
- branch creation;
- commit;
- push;
- PR creation;
- merge;
- project cancellation;
- human-gate resolution.

---

## 3.4 Heavy work should run externally where practical

The Syntra host should orchestrate rather than become a general-purpose build farm.

Heavy or platform-specific work should normally be delegated to:

- GitHub Actions;
- remote services;
- target-platform CI runners.

The Raspberry Pi should remain focused on orchestration, local Git workspaces, Codex execution, persistence, and messaging.

---

## 3.5 Restart recovery is a first-class feature

Every component that causes external side effects must support reconciliation.

Syntra must be able to determine what actually happened after:

- process crash;
- host reboot;
- network interruption;
- API timeout.

---

# 4. High-Level Architecture

```text
                          Human
                         iPhone
                            │
                            ▼
                    ┌──────────────┐
                    │   Telegram   │
                    └──────┬───────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                       SYNTRA BUILD                          │
│                                                             │
│  ┌──────────────────┐                                       │
│  │ Messaging Gateway│                                       │
│  └────────┬─────────┘                                       │
│           │                                                 │
│           ▼                                                 │
│  ┌──────────────────────────────┐                           │
│  │ Command / Intent Router      │                           │
│  └──────────────┬───────────────┘                           │
│                 │                                           │
│                 ▼                                           │
│  ┌──────────────────────────────┐                           │
│  │ Orchestration Engine         │                           │
│  │ State Machine + Workflow     │                           │
│  └──────┬────────┬────────┬─────┘                           │
│         │        │        │                                 │
│         │        │        │                                 │
│         ▼        ▼        ▼                                 │
│  ┌───────────┐ ┌───────────┐ ┌──────────────┐               │
│  │ Scheduler │ │ Gatekeeper│ │ Recovery Mgr │               │
│  └─────┬─────┘ └───────────┘ └──────────────┘               │
│        │                                                     │
│  ┌─────┴───────────────────────────────────────────────┐     │
│  │                  Worker Adapters                    │     │
│  │                                                     │     │
│  │  Architect   Codex   Git   GitHub   CI   Messaging │     │
│  └──────┬────────┬───────┬─────┬───────┬──────────────┘     │
│         │        │       │     │       │                    │
│         ▼        ▼       ▼     ▼       ▼                    │
│      OpenAI    Codex    Local  GitHub  GitHub                │
│      API       CLI      Git    API     Actions               │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Persistent State Store                              │    │
│  │ Projects / Milestones / Jobs / Gates / Events      │    │
│  │ Messages / Reviews / Attempts / External IDs       │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Local Workspace Manager                            │    │
│  │ Bare clones / worktrees / temporary artifacts      │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Observability                                      │    │
│  │ Logs / Metrics / Audit Trail                       │    │
│  └──────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘

                 Source repositories
                         │
                         ▼
                      GitHub
                 ┌───────┴────────┐
                 │                │
          Syntra Build      Generated projects
```

---

# 5. Core Components

The initial architecture consists of the following logical components:

1. Messaging Gateway
2. Command and Intent Router
3. Orchestration Engine
4. State Machine Service
5. Scheduler
6. Architect Adapter
7. Codex Runner
8. Workspace Manager
9. Git Manager
10. GitHub Adapter
11. CI Monitor
12. Gatekeeper
13. Human Gate Manager
14. Recovery Manager
15. Status Service
16. Persistence Layer
17. Observability Layer
18. Configuration and Secrets Manager

These are logical components.

The initial implementation does not require them to run as separate processes or containers.

---

# 6. Deployment Model

The preferred initial deployment is:

```text
Raspberry Pi Syntra Host
│
├── Syntra Build application
├── SQLite database
├── local Git repositories/worktrees
├── Codex CLI
├── Git CLI
├── GitHub CLI or GitHub API client
└── configuration / secrets
```

The preferred initial software architecture is a single Python application with internal modules.

The reasons are:

- low operational complexity;
- straightforward recovery;
- simple local debugging;
- low resource overhead;
- easy deployment;
- no need for distributed messaging;
- suitable scale for a small number of concurrent projects.

The architecture should nevertheless retain clean internal interfaces so components can be separated later if required.

---

# 7. Messaging Gateway

## 7.1 Responsibility

The Messaging Gateway connects Syntra Build to the human-facing messaging platform.

The initial platform is Telegram.

The gateway is responsible for:

- receiving inbound messages;
- sending outbound messages;
- maintaining messaging identifiers;
- associating messages with projects;
- handling message delivery failures;
- receiving attachments where supported;
- providing message-thread context;
- presenting human gates;
- returning status responses.

---

## 7.2 Messaging is not workflow state

Telegram history must not be treated as the authoritative project record.

Messages are inputs and outputs.

Workflow state remains in Syntra's persistent state store.

---

## 7.3 Project routing

Where the messaging platform supports project-specific threads or topics, Syntra should associate each project with a dedicated conversation context.

This reduces ambiguity between concurrent projects.

Syntra should still support project resolution from natural language where possible.

---

# 8. Command and Intent Router

## 8.1 Responsibility

The Command and Intent Router determines what an incoming human message represents.

Typical intents include:

```text
CREATE_PROJECT
PROJECT_DESIGN_MESSAGE
PROJECT_STATUS_QUERY
LIST_ACTIVE_PROJECTS
LIST_HUMAN_ACTIONS
HUMAN_GATE_RESPONSE
PAUSE_PROJECT
RESUME_PROJECT
CANCEL_PROJECT
PROJECT_CHANGE_REQUEST
GENERAL_SYSTEM_QUERY
```

---

## 8.2 Deterministic commands versus AI interpretation

Where possible, explicit commands should be handled deterministically.

Examples:

> Pause FlowTrack

> Resume FlowTrack

> What is the state of FlowTrack?

Natural-language interpretation may assist with flexible phrasing, but state-changing commands must still pass deterministic validation before execution.

---

# 9. Orchestration Engine

## 9.1 Responsibility

The Orchestration Engine is the central control component.

It is responsible for:

- evaluating current state;
- accepting workflow events;
- validating transitions;
- creating jobs;
- advancing milestones;
- advancing projects;
- invoking human gates;
- determining retry/escalation behaviour;
- coordinating external adapters;
- persisting transitions.

---

## 9.2 No long-running memory assumption

The Orchestration Engine must be able to reconstruct all necessary project context from persisted data.

It must not depend on in-memory AI conversation state for correctness.

---

## 9.3 Event-driven operation

The Orchestration Engine should react to events such as:

```text
MESSAGE_RECEIVED
DESIGN_APPROVED
ARCHITECT_TASK_READY
CODEX_COMPLETED
CI_PASSED
CI_FAILED
ARCHITECT_APPROVED
ARCHITECT_CHANGES_REQUIRED
HUMAN_TEST_PASSED
HUMAN_TEST_FAILED
HUMAN_DECISION_RECEIVED
MERGE_SUCCEEDED
RETRY_EXHAUSTED
```

These events drive the state transitions defined in `STATE_MACHINE.md`.

---

# 10. State Machine Service

The State Machine Service provides the authoritative rules for project, milestone, job, and human-gate transitions.

Responsibilities include:

- transition validation;
- transition guards;
- state-history creation;
- invariant checks;
- resume-state handling;
- blocked-state recovery targets;
- terminal-state protection.

The service must reject illegal transitions.

---

# 11. Scheduler

## 11.1 Responsibility

The Scheduler controls when executable jobs may run.

It is responsible for:

- queue management;
- concurrency limits;
- worker allocation;
- fairness;
- delayed retries;
- priority;
- pause awareness;
- project isolation.

---

## 11.2 Initial concurrency model

The initial system should support multiple concurrent projects but limit expensive local work.

A reasonable starting policy is:

```text
Architect calls:       maximum 2 concurrent
Codex runs:            maximum 2 concurrent
Repository provisioning: maximum 1 concurrent
Privileged merge actions: maximum 1 concurrent
CI monitoring:         lightweight / many concurrent
Human waits:           unlimited
```

These values should be configurable.

---

## 11.3 Fairness

A repeatedly failing project must not monopolise worker capacity.

The initial scheduler should favour simple fair scheduling, such as round-robin or bounded per-project queue consumption.

---

# 12. Architect Adapter

## 12.1 Responsibility

The Architect Adapter provides Syntra's interface to the AI software architect.

It is responsible for:

- design conversations;
- requirements analysis;
- specification generation;
- milestone task generation;
- PR review;
- change analysis;
- human-test recommendations;
- human-decision requests.

---

## 12.2 Structured outputs

Where Architect output drives workflow decisions, the adapter should require structured responses.

Examples include:

```text
APPROVE
CHANGES_REQUIRED
HUMAN_TEST_REQUIRED
HUMAN_DECISION_REQUIRED
BLOCKED
```

The adapter must validate required fields before passing results to the Orchestration Engine.

---

## 12.3 Context assembly

Syntra, not the Architect, is responsible for assembling the context provided with each request.

Depending on the operation, context may include:

- project description;
- current `SPEC.md`;
- current `AGENTS.md`;
- milestone;
- prior decisions;
- PR diff;
- CI results;
- previous review findings;
- user feedback.

---

## 12.4 Conversation continuity

Syntra may use provider conversation/session identifiers where useful, but they must be treated as an optimisation only.

If a provider conversation is lost, Syntra must be able to reconstruct the relevant context from persisted data.

---

# 13. Codex Runner

## 13.1 Responsibility

The Codex Runner executes coding-agent work inside a controlled local project workspace.

Codex is responsible for implementation.

Syntra remains responsible for repository control.

---

## 13.2 Codex input

The Codex Runner receives:

- assigned worktree;
- Architect-generated task;
- project instructions;
- `AGENTS.md`;
- milestone details;
- permitted environment information;
- relevant failure or review feedback.

---

## 13.3 Codex permissions

Codex may be permitted to:

- inspect repository files;
- edit repository files;
- create files;
- remove files where required;
- run tests;
- run linters;
- run local build commands;
- inspect Git status;
- inspect Git diff.

Codex must not receive credentials that allow it to:

- push to GitHub;
- create GitHub repositories;
- create GitHub PRs;
- merge PRs;
- change branch protection;
- modify repository administration;
- modify Syntra workflow state.

---

## 13.4 Codex output

The principal Codex deliverable is the changed local worktree.

A structured summary should also be captured containing:

- work completed;
- tests run;
- test results;
- known issues;
- files materially changed;
- completion assessment.

The summary is advisory.

The filesystem diff is authoritative.

---

## 13.5 Process isolation

The Codex process must run with:

- a dedicated working directory;
- restricted environment variables;
- no GitHub administrative credentials;
- execution time limits;
- process tracking;
- captured stdout/stderr;
- safe termination support.

Further sandboxing will be defined in `SECURITY.md`.

---

# 14. Workspace Manager

## 14.1 Responsibility

The Workspace Manager owns local project working copies.

It is responsible for:

- project repository clones;
- worktree creation;
- branch/worktree mapping;
- workspace cleanup;
- filesystem identity validation;
- repository reconciliation after restart.

---

## 14.2 Preferred Git layout

A project may use a local layout similar to:

```text
/var/lib/syntra-build/projects/
    flowtrack/
        repo.git/
        worktrees/
            m01/
            m02/
```

Exact paths will be defined later.

A bare or managed base repository with Git worktrees is preferred because it keeps milestone workspaces isolated and efficient.

---

## 14.3 Workspace ownership

Only Syntra creates or destroys worktrees.

Codex receives a path but does not manage worktree lifecycle.

---

# 15. Git Manager

## 15.1 Responsibility

The Git Manager performs deterministic local Git operations.

Typical responsibilities include:

- clone;
- fetch;
- branch creation;
- worktree creation;
- status;
- diff;
- add;
- commit;
- push;
- branch cleanup;
- merge-result verification.

---

## 15.2 Commit authority

Syntra, not Codex, creates commits.

This gives Syntra control over:

- commit identity;
- commit message;
- exact staged contents;
- branch;
- timing;
- audit trail.

---

## 15.3 Pre-commit validation

Before commit, Syntra must verify:

- correct repository;
- correct worktree;
- correct branch;
- expected base commit;
- non-empty diff;
- no prohibited path traversal;
- no known secrets;
- no policy violation.

---

# 16. GitHub Adapter

## 16.1 Responsibility

The GitHub Adapter performs privileged operations against generated project repositories.

Responsibilities include:

- repository creation;
- repository visibility;
- repository settings;
- branch protection;
- PR creation;
- PR lookup;
- PR metadata;
- merge;
- branch deletion;
- workflow metadata;
- release metadata where applicable.

---

## 16.2 Repository visibility

Repositories are public by default.

A repository is private only when private visibility was explicitly requested and approved during design.

The GitHub Adapter must use the persisted approved visibility rather than infer visibility at provisioning time.

---

## 16.3 Authentication

GitHub credentials belong to Syntra Build.

They must not be exposed to Codex or the Architect.

Exact credential scopes will be defined in `SECURITY.md`.

---

# 17. CI Monitor

## 17.1 Responsibility

The CI Monitor tracks required GitHub Actions checks for the active PR.

It is responsible for:

- discovering required checks;
- tracking running checks;
- determining pass/fail state;
- collecting failure information;
- identifying transient failures where possible;
- reporting results to the Orchestration Engine.

---

## 17.2 No busy waiting

The CI Monitor must not consume a scarce worker slot while waiting.

Implementation may use:

- periodic polling;
- scheduled reconciliation;
- webhooks in future.

The initial design should favour simplicity and reliability.

---

# 18. Gatekeeper

## 18.1 Responsibility

The Gatekeeper performs deterministic checks before privileged transitions.

It is particularly important before merge.

The Gatekeeper must not call an AI to determine whether merge policy is satisfied.

---

## 18.2 Merge checks

At minimum, merge validation must confirm:

- expected project;
- expected repository;
- expected PR;
- expected branch;
- PR remains open;
- current PR head SHA matches reviewed SHA;
- all required CI checks pass;
- Architect verdict is `APPROVE`;
- no unresolved critical findings exist;
- all required human gates are resolved;
- project is not paused;
- project is not cancelled;
- milestone is in `MERGE_READY`;
- no blocking system condition exists.

Only then may Syntra invoke the GitHub merge operation.

---

# 19. Human Gate Manager

## 19.1 Responsibility

The Human Gate Manager creates and resolves explicit user-action requirements.

Gate types include:

- design approval;
- product decision;
- technical decision;
- human test;
- final acceptance;
- recovery decision.

---

## 19.2 Gate correlation

Every gate must have a unique identifier and be associated with:

- project;
- milestone where applicable;
- originating workflow;
- messaging context;
- expected response type;
- resume target.

This prevents an unrelated user message from accidentally resolving the wrong gate.

---

# 20. Status Service

## 20.1 Responsibility

The Status Service answers human queries about project and system state.

Examples:

> What is happening with FlowTrack?

> What projects are active?

> Is anything waiting for me?

---

## 20.2 Data sources

Status must primarily come from Syntra's persisted state.

For fast-changing external information such as GitHub Actions, the service may perform lightweight reconciliation before replying.

---

## 20.3 Status model

The service should distinguish:

- state;
- activity;
- human action;
- next action.

Example:

```text
Project: FlowTrack
State: BUILDING
Milestone: M4
Milestone state: CI_RUNNING
Activity: Waiting for macOS package job
Human action: None
Next: Architect review
```

The messaging response may then convert this into natural prose.

---

# 21. Recovery Manager

## 21.1 Responsibility

The Recovery Manager reconciles persisted state with actual external state after application or host restart.

It is responsible for inspecting:

- running local processes;
- Git worktrees;
- branches;
- commits;
- GitHub PRs;
- CI runs;
- human gates;
- unfinished jobs.

---

## 21.2 Recovery is reconciliation, not replay

Syntra must not blindly repeat the last requested action.

Example:

Persisted state:

```text
PR_CREATING
```

On restart, Syntra first queries GitHub.

If the PR already exists, it records that result and continues to `CI_RUNNING`.

Only if the PR does not exist should creation be retried.

---

# 22. Persistence Layer

## 22.1 Initial database

The preferred initial database is SQLite.

SQLite is appropriate because:

- the workload is low-volume;
- there is a single Syntra Build service;
- durable transactions are required;
- operational complexity should remain low;
- the Raspberry Pi host is resource constrained;
- backup is straightforward.

WAL mode should be considered for concurrent reads and writes.

---

## 22.2 Persisted entities

The state store will need to represent at least:

```text
projects
project_documents
project_decisions
milestones
jobs
job_attempts
human_gates
state_transitions
workflow_events
messages
architect_requests
architect_responses
codex_runs
git_workspaces
commits
pull_requests
ci_runs
reviews
errors
notifications
```

The exact data model will be defined in `DATA_MODEL.md`.

---

## 22.3 External identifiers

Syntra should persist external identifiers rather than relying on names alone.

Examples:

- Telegram chat ID;
- Telegram thread ID;
- GitHub repository ID;
- GitHub PR number;
- GitHub workflow run ID;
- Git commit SHA;
- Architect provider request/conversation ID.

---

# 23. Project Documents

## 23.1 Source-of-truth documents

Every project has:

- `SPEC.md`;
- `AGENTS.md`.

During design, working drafts may exist in Syntra's state/artifact store before the GitHub repository is created.

After approval and repository provisioning, the approved documents are committed to GitHub.

---

## 23.2 Versioning

Changes made after design approval must be versioned.

Syntra should retain:

- approved revision;
- later revisions;
- reason for change;
- approval record;
- affected milestones.

The detailed change-control process will be defined later.

---

# 24. Configuration

Configuration should be separate from project data.

Expected categories include:

```text
messaging
architect provider
codex
github
git
scheduler
retry limits
timeouts
filesystem paths
logging
metrics
security
```

Configuration should support environment-specific overrides without code changes.

---

# 25. Secrets Management

Secrets must not be stored in:

- GitHub project repositories;
- GitHub repositories;
- Telegram messages;
- `SPEC.md`;
- `AGENTS.md`;
- normal application logs.

Initial secrets may be loaded from host-protected files or environment variables.

A more advanced secret-management system may be introduced later if justified.

Detailed secret handling belongs in `SECURITY.md`.

---

# 26. Trust Boundaries

The architecture contains several important trust boundaries.

```text
Human / Telegram
      │
      ▼
Messaging Boundary
      │
      ▼
Syntra Control Plane
      │
      ├─────────────▶ Architect AI
      │
      ├─────────────▶ Codex AI
      │
      └─────────────▶ GitHub
```

Syntra must not assume that any external response is correct merely because it came from a trusted service.

All externally supplied data must be validated against local expected context.

---

# 27. Architect Trust Model

The Architect is trusted to:

- reason about design;
- generate tasks;
- review implementation;
- recommend decisions.

The Architect is not trusted to:

- change workflow state directly;
- merge code;
- alter repositories;
- bypass tests;
- resolve human gates;
- authorize its own privileged actions.

---

# 28. Codex Trust Model

Codex is trusted to:

- modify files in its assigned workspace;
- run permitted project commands;
- produce tests and implementation.

Codex is not trusted to:

- control GitHub;
- manage project state;
- change repository administration;
- access Syntra secrets;
- decide whether its own code should merge.

---

# 29. GitHub Trust Model

GitHub is the canonical remote repository for generated projects.

Syntra should nevertheless verify:

- repository identity;
- branch identity;
- PR identity;
- commit SHA;
- CI result;
- merge result.

GitHub responses are authoritative about GitHub state, but Syntra remains authoritative about workflow intent.

---

# 30. Failure Isolation

Failures should be isolated by project.

A failure in one project must not terminate the Syntra Build service or block unrelated projects.

Examples:

```text
Project A → BLOCKED
Project B → CI_RUNNING
Project C → CODING
Project D → DESIGNING
```

The scheduler and state store must support these simultaneously.

---

# 31. Error Classification

Errors should be classified broadly as:

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

This allows the Orchestration Engine to apply appropriate policy.

For example:

- temporary GitHub timeout → retry;
- invalid Codex diff → return to Codex;
- wrong repository identity → block;
- corrupted database → fatal/system intervention.

---

# 32. Observability

## 32.1 Logs

Syntra Build should produce structured logs containing:

- timestamp;
- project ID;
- milestone ID;
- job ID;
- correlation ID;
- component;
- event;
- severity.

Secrets must be redacted.

---

## 32.2 Metrics

Syntra Build should expose Prometheus-compatible metrics for the existing Syntra monitoring stack.

Useful metrics may include:

```text
active_projects
active_codex_jobs
active_architect_jobs
queued_jobs
blocked_projects
waiting_human_projects
job_duration
codex_attempts
architect_review_cycles
ci_failures
github_api_failures
state_transition_count
```

---

## 32.3 Audit history

Important actions should remain queryable after completion.

Examples:

- design approved by human;
- repository created;
- Codex attempt started;
- PR created;
- Architect rejected implementation;
- human test passed;
- merge performed.

---

# 33. Filesystem Architecture

The exact layout will be defined later, but a likely initial structure is:

```text
/opt/syntra-build/
    app/
    config/

/var/lib/syntra-build/
    syntra-build.db
    projects/
        <project>/
            repo.git/
            worktrees/
    artifacts/

/var/log/syntra-build/
```

Secrets should use a protected path separate from application source.

---

# 34. Process Model

The preferred initial model is a single long-running Syntra Build service.

Internally it may use asynchronous tasks for:

- Telegram polling;
- scheduler;
- Architect calls;
- Codex workers;
- CI monitoring;
- retry timers;
- reconciliation.

This avoids the need for:

- RabbitMQ;
- Redis queues;
- Celery;
- Kubernetes;
- distributed workers.

These should only be introduced if scale or reliability requirements later justify them.

---

# 35. Internal Communication Model

Because the initial system is a single application, internal components should communicate through:

- typed function/service interfaces;
- domain events;
- persisted jobs;
- state transitions.

Internal HTTP APIs are not required for version one.

This keeps latency and complexity low while retaining clear module boundaries.

---

# 36. External Communication Model

External integrations initially include:

```text
Telegram API
OpenAI Architect API
Codex CLI
GitHub API / CLI
Git command-line client
GitHub Actions
```

All external integrations should sit behind adapters.

Core orchestration code should not directly embed provider-specific calls.

---

# 37. Dependency Direction

A preferred internal dependency direction is:

```text
Messaging / External adapters
            ↓
Application Services
            ↓
Domain / State Machine
            ↓
Persistence Interfaces
```

The state-machine/domain layer should not import Telegram, GitHub, or OpenAI-specific code.

This improves testability and future replacement of integrations.

---

# 38. Suggested Module Boundaries

A possible Python package structure is:

```text
syntra_build/
    domain/
        projects/
        milestones/
        jobs/
        gates/
        events/
        state_machine/

    application/
        orchestrator/
        scheduler/
        status/
        recovery/
        gatekeeper/

    adapters/
        telegram/
        architect/
        codex/
        github/
        git/

    infrastructure/
        persistence/
        config/
        logging/
        metrics/
        processes/
        filesystem/
```

This is illustrative rather than mandatory.

The final package structure will be defined in `SPEC.md`.

---

# 39. Repository Architecture

Syntra Build itself and generated projects will be stored in GitHub.

```text
GitHub
├── syntra-build
├── project-a
├── project-b
├── project-c
└── ...
```

This allows the same Architect, Codex, pull-request, CI and review workflow to be used when developing Syntra Build itself.

The separation between Syntra Build and generated projects remains a logical and security boundary rather than a source-control-platform boundary.

When Codex works on Syntra Build, it receives a controlled local worktree in exactly the same manner as other projects. It still receives no privileged GitHub credentials, and Syntra retains control of commits, pushes, PR creation and merge.

---

# 40. Repository Provisioning Architecture

Repository provisioning should occur only after design approval.

The flow is:

```text
Approved project design
       ↓
Syntra provisioning job
       ↓
GitHub Adapter
       ↓
Create repository
       ↓
Apply visibility
       ↓
Initialise local repository
       ↓
Commit SPEC.md + AGENTS.md
       ↓
Push main
       ↓
Apply repository policy
       ↓
Verify
```

Public visibility is the default.

Private visibility must be explicitly present in approved project data.

---

# 41. Implementation Milestone Architecture

Each milestone should correspond to one controlled development branch and normally one GitHub PR.

```text
main
  │
  ├── syntra/m01-...
  │        ↓
  │      PR #1
  │        ↓
  │      merge
  │
  ├── syntra/m02-...
  │        ↓
  │      PR #2
  │        ↓
  │      merge
```

A project should initially execute only one implementation milestone at a time.

This avoids repository-level concurrency conflicts while still allowing multiple projects to build in parallel.

---

# 42. PR Lifecycle Architecture

The PR lifecycle is controlled entirely by Syntra.

```text
Syntra creates branch/worktree
       ↓
Codex edits files
       ↓
Syntra validates
       ↓
Syntra commits
       ↓
Syntra pushes
       ↓
Syntra creates PR
       ↓
GitHub Actions
       ↓
Architect review
       ↓
rework if needed
       ↓
Gatekeeper
       ↓
Syntra merge
```

Codex never directly creates or updates the GitHub PR.

It indirectly updates the PR only because Syntra commits and pushes its revised files to the existing branch.

---

# 43. Human Interaction Architecture

Human interaction should be event-driven and non-blocking for other projects.

Example:

```text
Project A
HUMAN_TEST
    │
    └──── waiting

Project B
CODING
    │
    └──── continues

Project C
CI_RUNNING
    │
    └──── continues
```

Human gates do not consume Codex or Architect worker capacity.

---

# 44. Status Query Architecture

A status query follows a separate read path:

```text
Telegram message
      ↓
Intent Router
      ↓
Status Service
      ↓
State Store
      │
      ├── optional GitHub reconciliation
      │
      ▼
Status Projection
      ↓
Telegram response
```

This path should not interrupt or mutate the active project workflow.

---

# 45. Recovery Architecture

Startup recovery should follow:

```text
Service start
    ↓
Load incomplete projects
    ↓
Load active milestones/jobs/gates
    ↓
Reconcile local workspaces
    ↓
Reconcile GitHub state
    ↓
Reconcile external jobs
    ↓
Mark unknown jobs abandoned where required
    ↓
Create safe replacement work
    ↓
Resume scheduler
```

Recovery should complete before privileged workflow actions resume.

---

# 46. Backup and Durability

The architecture should support backup of:

- SQLite database;
- configuration;
- project artifacts not already stored in Git;
- audit metadata.

Generated project source already pushed to GitHub does not need to be treated as unique local data.

Local worktrees containing unpushed Codex changes do represent recoverable but potentially unique work and should be considered in backup/recovery design.

Detailed backup policy belongs in `OPERATIONS.md`.

---

# 47. Upgrade Strategy

Application upgrades should not require project state loss.

The architecture should therefore support:

- database migrations;
- backward-compatible persisted state where practical;
- graceful shutdown;
- job reconciliation after upgrade;
- state-machine versioning if transitions change materially.

---

# 48. Testing Architecture

The orchestration system should itself be highly testable.

Important tests include:

- state-transition tests;
- invalid-transition tests;
- scheduler fairness;
- retry policy;
- human-gate correlation;
- merge gatekeeper logic;
- recovery idempotency;
- GitHub adapter mocks;
- Codex runner failure handling;
- status projection;
- multi-project isolation.

The core state machine should be testable without real Telegram, GitHub, OpenAI, or Codex connections.

---

# 49. Initial Technology Choices

The current preferred initial technologies are:

| Area | Choice |
|---|---|
| Implementation language | Python 3.12 |
| Deployment | Single service on Syntra |
| Persistence | SQLite |
| Messaging | Telegram |
| Architect | OpenAI API through adapter |
| Coding agent | Codex CLI through adapter |
| Local source control | Git |
| Generated project remote | GitHub |
| CI/CD | GitHub Actions |
| Syntra Build source | GitHub |
| Monitoring | Prometheus + Grafana |
| Logging | Structured local logs |

These are architecture defaults rather than permanent constraints.

Where possible, replaceability should be preserved through adapters.

---

# 50. Deferred Complexity

The initial architecture deliberately does not include:

- microservices;
- Redis;
- RabbitMQ;
- Celery;
- Kubernetes;
- distributed workers;
- multiple active milestones in one repository;
- custom web UI;
- custom messaging application;
- self-hosted CI runners;
- public multi-user authentication;
- complex role-based access control;
- general-purpose plugin orchestration.

These may be reconsidered if later requirements justify them.

---

# 51. Future Extension Points

Likely future extension points include:

- additional messaging platforms;
- alternative Architect models;
- alternative coding agents;
- preview deployment providers;
- deployment/release providers;
- additional Git hosts;
- parallel milestones;
- self-hosted runners;
- richer web-based operational UI;
- mobile-friendly project dashboards;
- artifact management;
- cost tracking;
- AI usage budgets.

These should not complicate version one unnecessarily.

---

# 52. Architecture Invariants

The following architectural rules must always hold:

1. Syntra owns authoritative workflow state.
2. AI agents never directly alter workflow state.
3. Codex never receives privileged GitHub credentials.
4. Codex never merges code.
5. Syntra controls commits and pushes.
6. Syntra creates and manages GitHub PRs.
7. Merge requires deterministic Gatekeeper approval.
8. Human approvals are explicit and durable.
9. Project status is queryable from persisted state.
10. Projects are isolated from one another.
11. Restart recovery reconciles before replaying external actions.
12. GitHub repositories are public by default unless private was explicitly approved.
13. Generated projects live in GitHub.
14. Syntra Build source lives in GitHub.
15. Heavy cross-platform build work should normally run in GitHub Actions.

---

# 53. Architecture Completion Criteria

The architectural design is sufficiently complete when:

- every workflow responsibility has an owning component;
- all privileged operations have a clear trust boundary;
- Architect and Codex responsibilities are separated;
- local and remote repository ownership is defined;
- persistence responsibilities are defined;
- recovery behaviour is supported;
- human gates are represented independently of AI workers;
- multi-project scheduling is supported;
- status queries have a defined read path;
- deployment fits the Syntra Raspberry Pi host;
- external providers sit behind replaceable adapters;
- implementation can proceed without inventing new control-plane responsibilities.

The next design documents should refine this architecture into:

- `INTERFACES.md`
- `DATA_MODEL.md`
- `SECURITY.md`
- `OPERATIONS.md`

Those documents will provide the remaining detail required before `SPEC.md` and `AGENTS.md` are written.
