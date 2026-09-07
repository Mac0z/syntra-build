# Syntra Build — Workflow

## 1. Purpose

This document defines the operational workflow used by Syntra Build to take a software project from initial idea through design, implementation, review, testing and completion.

It describes:

- how projects are created;
- how design conversations progress;
- how `SPEC.md` and `AGENTS.md` are produced and approved;
- how repositories are provisioned;
- how milestones are selected and executed;
- how Codex is invoked;
- how GitHub pull requests are created and updated;
- how CI failures are handled;
- how Architect review is performed;
- how human decisions and testing are requested;
- how milestones are merged;
- how projects progress;
- how status queries are answered;
- how failures and retries are handled; and
- how multiple projects operate concurrently.

This document describes behaviour and control flow.

Formal state definitions and allowed state transitions will be defined separately in `STATE_MACHINE.md`.

---

# 2. Workflow Principles

The workflow is governed by the following principles.

## 2.1 Syntra owns the process

Syntra Build is the authoritative controller of project workflow.

Neither the Architect nor Codex decides independently what stage a project is in or what action should occur next.

Syntra determines:

- current project state;
- current milestone;
- allowed next actions;
- retry count;
- whether human intervention is required;
- whether a pull request may be merged; and
- whether the project may advance.

---

## 2.2 AI output is advisory until validated

Architect and Codex outputs are inputs to the workflow.

They are not privileged commands.

For example:

- an Architect `APPROVE` response does not itself merge a PR;
- a Codex claim that tests pass does not replace CI;
- a Codex completion response does not automatically result in a commit;
- an Architect request for a human decision must be converted into a formal human gate by Syntra.

Syntra validates AI outputs and applies workflow policy before acting on them.

---

## 2.3 Human involvement is explicit

Human intervention should occur only through a defined human gate.

A project should never become implicitly blocked waiting for a human.

Syntra must record:

- what is required;
- why it is required;
- what project and milestone it applies to;
- what response is expected; and
- when the request was issued.

---

## 2.4 Work should be resumable

Every material workflow transition must be persisted before the next external action begins.

The system should always be able to recover after:

- service restart;
- Syntra host restart;
- network interruption;
- AI request failure;
- GitHub API failure;
- CI delay;
- Telegram interruption; or
- Codex process failure.

---

## 2.5 Projects are independent

A blocked, failed or paused project must not prevent unrelated projects from progressing.

Work is scheduled per project and subject to global concurrency limits.

---

# 3. Principal Workflow Participants

## Human Project Owner

Provides:

- initial project intent;
- design feedback;
- design approval;
- important product decisions;
- human test results;
- manual verification where required.

---

## Architect

Provides:

- requirements analysis;
- architectural guidance;
- `SPEC.md`;
- `AGENTS.md`;
- milestone definitions;
- Codex implementation instructions;
- PR reviews;
- change requests;
- human-decision requests;
- human-test requests;
- milestone approval.

---

## Codex

Provides:

- source-code changes;
- test changes;
- documentation changes;
- local validation;
- fixes for CI failures;
- fixes for Architect review findings.

Codex works only inside the workspace assigned by Syntra.

---

## Syntra Build

Controls:

- project records;
- project state;
- conversation routing;
- scheduling;
- AI invocation;
- workspaces;
- Git operations;
- GitHub operations;
- CI monitoring;
- retry handling;
- human gates;
- merging;
- progression;
- status reporting.

---

## GitHub

Provides:

- canonical generated-project repository;
- branches;
- pull requests;
- CI/CD;
- build artifacts;
- release infrastructure where applicable.

---

# 4. Project Creation Workflow

## 4.1 User initiates a project

A new project begins through the messaging platform.

Example:

> Create a new project called FlowTrack. I want a desktop application that...

Syntra identifies this as a project-creation request.

Syntra must extract or determine:

- proposed project name;
- initial project description;
- initiating user;
- messaging conversation or project thread;
- creation timestamp.

---

## 4.2 Project name validation

Before the project is created, Syntra validates the proposed project name.

Validation should include:

- required name present;
- name meets repository naming rules;
- no existing active Syntra project with the same canonical name;
- no conflicting GitHub repository owned by Syntra Build;
- no reserved system name.

If the name is invalid or ambiguous, Syntra requests clarification through the messaging platform.

No GitHub repository is created at this stage.

---

## 4.3 Create project record

Once the name is valid, Syntra creates the internal project record.

Initial persisted information should include:

- project identifier;
- project name;
- canonical repository name;
- current project state;
- initial request;
- messaging context;
- creation time;
- current design conversation reference;
- project owner.

The project enters design mode.

---

# 5. Design Conversation Workflow

## 5.1 Architect receives initial design context

Syntra sends the Architect:

- initial user request;
- project name;
- relevant previous design messages;
- any known global Syntra Build constraints.

The Architect begins requirements analysis.

---

## 5.2 Conversational design loop

The Architect may:

- ask questions;
- propose options;
- identify assumptions;
- recommend architecture;
- identify risks;
- challenge conflicting requirements;
- suggest scope changes.

Syntra relays these messages through the messaging platform.

User responses are associated with the correct project and returned to the Architect.

This loop continues until the design is considered sufficiently complete.

---

## 5.3 Design conversation persistence

The design conversation must not exist only inside an AI session.

Syntra should persist sufficient design history to allow the Architect session to be recreated if necessary.

This may include:

- user messages;
- Architect messages;
- decisions;
- assumptions;
- resolved questions;
- unresolved questions;
- key architecture choices.

---

## 5.4 Repository visibility decision

GitHub repositories created by Syntra Build should be **public by default**.

During the initial design conversation, the Architect should determine whether there is any reason for the project repository to be private.

If the user explicitly requests a private repository, or explicitly approves privacy as part of the design, that requirement must be recorded in the project specification before repository creation.

If no privacy requirement is stated, Syntra should provision the repository as public.

Repository visibility is therefore treated as a design decision with the following default:

```text
visibility = public
```

A private repository requires explicit instruction.

---

## 5.5 Draft specification generation

When the Architect determines that the project is ready for formalisation, it produces:

- `SPEC.md`;
- `AGENTS.md`;
- a concise design summary;
- any remaining assumptions;
- any unresolved but non-blocking issues.

Syntra stores these documents as the current design draft.

---

# 6. Design Approval Workflow

## 6.1 Present design for approval

The user receives a message indicating that the formal design is ready.

The message should include:

- project name;
- concise summary;
- major architectural choices;
- planned repository visibility;
- number of planned milestones;
- important constraints;
- any notable assumptions.

The user should be able to:

- approve;
- request changes;
- ask questions.

---

## 6.2 Design changes requested

If the user requests changes:

1. Syntra records the feedback.
2. The project remains in design mode.
3. The Architect receives the feedback and current draft.
4. The Architect updates the design.
5. Updated `SPEC.md` and/or `AGENTS.md` are generated.
6. The approval workflow repeats.

---

## 6.3 Design approved

Once the user explicitly approves:

- the current `SPEC.md` becomes the approved project specification;
- the current `AGENTS.md` becomes the approved engineering instruction set;
- the approved repository visibility becomes authoritative;
- the approved versions are immutable historical artifacts;
- later changes must be versioned through a controlled change process.

The project may then enter repository provisioning.

---

# 7. Repository Provisioning Workflow

## 7.1 Create GitHub repository

Syntra creates the GitHub repository using the approved canonical project name.

Repositories are created as **public by default**.

A repository is created as private only if private visibility was explicitly requested and approved during the project design process.

Syntra must not infer privacy merely from project type, source contents or implementation language.

---

## 7.2 Initialise repository

Syntra creates the initial repository contents.

At minimum:

- `SPEC.md`;
- `AGENTS.md`;
- `.gitignore`;
- project README where appropriate;
- initial CI configuration where it can be determined at design time;
- repository metadata required by the project.

---

## 7.3 Configure repository policy

Syntra applies supported repository policies.

These may include:

- default branch;
- branch protection;
- required status checks;
- squash merge policy;
- deletion of merged branches;
- repository visibility;
- issue settings;
- Actions permissions.

Exact policy will be defined separately.

---

## 7.4 Initial commit

Syntra commits and pushes the approved design baseline to `main`.

The commit should clearly identify it as the initial approved design.

---

## 7.5 Provisioning verification

Before build mode begins, Syntra confirms:

- repository exists;
- repository identity matches the project;
- repository visibility matches the approved design;
- `main` exists;
- approved `SPEC.md` is present;
- approved `AGENTS.md` is present;
- repository is reachable;
- required CI configuration is present where applicable.

If verification fails, Syntra does not begin implementation.

---

# 8. Milestone Selection Workflow

## 8.1 Determine next milestone

Syntra reads the approved project plan and identifies the next incomplete milestone.

Each milestone should contain:

- unique milestone identifier;
- title;
- objective;
- requirements;
- dependencies;
- automated acceptance criteria;
- human acceptance criteria where required;
- expected deliverables;
- completion definition.

---

## 8.2 Dependency validation

Before starting a milestone, Syntra verifies that all declared dependencies are complete.

If not, the milestone cannot begin.

---

## 8.3 Milestone activation

Syntra records the milestone as active.

The active milestone becomes the unit of implementation, PR creation, CI, review and merge.

Only one implementation PR per milestone should normally be active at a time.

---

# 9. Architect Task Preparation Workflow

## 9.1 Architect receives milestone context

Syntra supplies the Architect with:

- current `SPEC.md`;
- current `AGENTS.md`;
- milestone definition;
- current repository state;
- relevant prior milestone summaries;
- known technical context;
- unresolved issues.

---

## 9.2 Architect creates Codex task

The Architect returns a structured implementation instruction.

It should include:

- project identifier;
- milestone identifier;
- objective;
- required changes;
- acceptance criteria;
- constraints;
- relevant architecture boundaries;
- likely files or components;
- tests expected;
- explicit exclusions;
- human-test expectations if relevant.

---

## 9.3 Task validation

Syntra validates that the Architect response:

- refers to the correct project;
- refers to the correct milestone;
- contains required fields;
- does not request prohibited privileged actions;
- does not contradict fixed project policy.

If invalid, Syntra requests a corrected task from the Architect.

---

# 10. Workspace Preparation Workflow

## 10.1 Synchronise repository

Syntra ensures its local repository is current with GitHub `main`.

---

## 10.2 Create milestone branch

Syntra creates a dedicated branch using a predictable naming scheme.

Example:

```text
syntra/m03-historical-price-chart
```

---

## 10.3 Create isolated worktree

A dedicated local Git worktree is created for the active milestone.

Codex is restricted to this workspace.

---

## 10.4 Record workspace metadata

Syntra persists:

- branch name;
- base commit;
- worktree path;
- milestone identifier;
- Codex attempt number.

---

# 11. Codex Implementation Workflow

## 11.1 Invoke Codex

Syntra invokes Codex with:

- Architect task;
- repository path;
- relevant project instructions;
- current milestone;
- environment restrictions.

Codex must not receive privileged GitHub credentials.

---

## 11.2 Codex works

Codex may:

- inspect files;
- edit files;
- create files;
- remove files when required;
- run local tests;
- run linters;
- run project build commands where permitted;
- inspect Git status and diff.

---

## 11.3 Codex completion response

When Codex finishes, it should return a structured summary including:

- work completed;
- files materially changed;
- tests run;
- test results;
- known limitations;
- unresolved issues;
- whether it believes the milestone is ready for submission.

This response is advisory.

The actual deliverable is the resulting Git workspace.

---

# 12. Post-Codex Validation Workflow

Before committing Codex changes, Syntra performs deterministic validation.

At minimum:

- worktree exists;
- project identity is correct;
- active branch is correct;
- base branch has not unexpectedly changed;
- Git diff is not empty;
- changes are within the expected repository;
- no prohibited system paths are affected;
- no GitHub credentials or known secrets have been introduced;
- no protected workflow files have been modified without permission;
- repository remains structurally valid.

Additional project-specific validation may also run.

---

## 12.1 Invalid Codex output

If validation fails:

- no push occurs;
- no PR is created;
- the failure is recorded.

Depending on the failure type, Syntra may:

- return the issue to Codex for correction;
- ask the Architect for guidance;
- escalate to the human owner.

---

# 13. Commit and Pull Request Workflow

## 13.1 Commit changes

If validation succeeds, Syntra creates the Git commit.

Syntra controls:

- commit author policy;
- commit message format;
- branch;
- push destination.

---

## 13.2 Push branch

Syntra pushes the milestone branch to GitHub.

Codex does not perform this action.

---

## 13.3 Create pull request

Syntra creates a real GitHub PR.

The PR should include:

- project;
- milestone;
- milestone objective;
- implementation summary;
- acceptance criteria;
- Codex attempt summary;
- human testing requirements where known.

---

## 13.4 Record PR information

Syntra stores:

- PR number;
- PR URL;
- branch;
- head commit;
- creation timestamp;
- CI status.

---

# 14. CI Workflow

## 14.1 Wait for GitHub Actions

After PR creation or update, Syntra monitors required GitHub Actions checks.

The project may remain in a waiting state without consuming an implementation worker slot.

---

## 14.2 CI passes

If all required checks pass, the milestone proceeds to Architect review.

---

## 14.3 CI fails

If one or more required checks fail, Syntra collects useful failure information.

This may include:

- failed workflow;
- failed job;
- failed test names;
- relevant error excerpts;
- build failure context;
- links to full GitHub logs.

---

# 15. CI Rework Workflow

## 15.1 Retry classification

Syntra determines whether the failure is likely to be:

- implementation failure;
- test failure;
- transient infrastructure failure;
- external dependency failure;
- configuration failure;
- unknown.

Where deterministic classification is not possible, the Architect may assist.

---

## 15.2 Transient failures

For clearly transient failures, Syntra may retry CI according to configured retry policy without invoking Codex.

---

## 15.3 Implementation failures

For implementation-related failures:

1. Syntra returns the CI findings to Codex.
2. Codex works in the same milestone worktree.
3. Syntra validates the new diff.
4. Syntra creates an additional commit.
5. Syntra pushes the same branch.
6. The existing PR updates automatically.
7. CI runs again.

The existing pull request remains the milestone's PR.

---

## 15.4 Retry limits

Syntra tracks retry attempts.

A milestone must not loop forever.

When configured retry thresholds are exceeded, the workflow escalates.

Escalation may go to:

- Architect analysis;
- human decision;
- project failure state.

---

# 16. Architect Review Workflow

## 16.1 Review begins only after CI passes

The Architect should normally review only a commit for which all required CI checks have passed.

Exceptions may be defined later for specific review types.

---

## 16.2 Review context

Syntra provides the Architect with:

- milestone requirements;
- acceptance criteria;
- `SPEC.md`;
- `AGENTS.md`;
- PR metadata;
- diff;
- relevant changed file contents;
- CI results;
- previous review findings if any.

---

## 16.3 Architect verdict

The Architect must return a structured verdict.

Permitted outcomes should include:

### APPROVE

The implementation satisfies the milestone.

### CHANGES_REQUIRED

The implementation requires further work.

### HUMAN_TEST_REQUIRED

Automated and architectural review is satisfactory, but human testing is needed before merge.

### HUMAN_DECISION_REQUIRED

A product, design or technical choice requires human input before work can continue.

### BLOCKED

The milestone cannot safely proceed because of an external or structural issue.

---

# 17. Architect Review Rework Workflow

If the Architect returns `CHANGES_REQUIRED`:

1. findings are persisted;
2. findings are converted into a Codex rework task;
3. Codex works in the same milestone worktree;
4. Syntra validates the changes;
5. Syntra commits;
6. Syntra pushes the existing branch;
7. CI runs again;
8. if CI passes, Architect review repeats.

This cycle continues until:

- approved;
- a human gate is required;
- retry limits are exceeded;
- the milestone becomes blocked.

---

# 18. Human Decision Workflow

## 18.1 Create decision gate

When a decision is required, Syntra creates a durable decision request.

It records:

- project;
- milestone;
- decision identifier;
- question;
- context;
- options where applicable;
- Architect recommendation where applicable;
- date requested.

---

## 18.2 Notify human

The messaging platform sends a concise request.

Example:

> FlowTrack M4 needs a design decision.
>
> The application can either store diagnostics locally only or retain the last five captures automatically.
>
> Architect recommendation: retain the last five.
>
> Which approach should we use?

---

## 18.3 Project waits safely

The affected project does not progress until the decision is resolved.

Other projects may continue.

---

## 18.4 Human responds

Syntra associates the response with the outstanding decision.

The answer is persisted.

It is then returned to the relevant workflow participant.

Usually:

- Architect updates instructions; or
- Codex continues with clarified requirements.

---

# 19. Human Testing Workflow

## 19.1 Create test gate

When human validation is required, Syntra creates a durable test request.

The request should define:

- project;
- milestone;
- build or PR being tested;
- test environment;
- test instructions;
- expected behaviour;
- what response is required.

---

## 19.2 Notify human

Example:

> FlowTrack M6 is ready for macOS testing.
>
> Please download build 6.2 and verify:
>
> 1. application launches normally;
> 2. diagnostic capture can be enabled;
> 3. closing and reopening preserves the setting.
>
> Reply PASS if all checks succeed, or describe any problems you find.

---

## 19.3 Human test passes

If the user confirms the test has passed, the gate is closed.

The milestone proceeds to merge validation.

---

## 19.4 Human test fails

The user may provide:

- description;
- screenshots;
- logs;
- observations;
- reproduction steps.

Syntra persists the result and provides it to the Architect.

The Architect determines the required Codex rework.

The normal PR revision cycle then resumes.

---

# 20. Pre-Merge Gatekeeper Workflow

Before merge, Syntra performs a final deterministic gate check.

At minimum:

- correct project;
- correct milestone;
- correct PR;
- PR still open;
- expected branch;
- current head commit reviewed;
- all required CI passed;
- Architect verdict is `APPROVE`;
- no unresolved Architect findings;
- no unresolved human decision;
- required human tests passed;
- no project pause;
- no known critical system error.

If any gate fails, merge is refused.

---

# 21. Merge Workflow

## 21.1 Merge approved PR

Syntra performs the merge using the configured repository merge strategy.

The initial preferred policy is squash merge.

---

## 21.2 Verify merge

Syntra verifies:

- PR merged successfully;
- expected commit exists on `main`;
- required branch policy succeeded.

---

## 21.3 Clean up

Syntra may:

- delete remote milestone branch;
- remove local worktree;
- prune local branch;
- archive milestone execution records.

Cleanup must occur only after successful merge verification.

---

# 22. Milestone Completion Workflow

Once merge is verified:

- milestone becomes complete;
- completion timestamp is stored;
- merged PR is recorded;
- milestone summary is stored;
- any human test evidence is linked;
- project progress is recalculated.

Syntra then determines whether another milestone remains.

---

# 23. Next Milestone Workflow

If another milestone exists:

1. dependency checks run;
2. next milestone becomes active;
3. Architect task preparation begins;
4. implementation workflow repeats.

No human approval should normally be required between milestones unless the specification explicitly defines one.

---

# 24. Project Completion Workflow

If all required milestones are complete, Syntra performs final project validation.

This may include:

- final CI;
- final release build;
- final human acceptance test;
- repository verification;
- release creation;
- documentation checks.

Exact requirements are project-specific.

Once final acceptance is satisfied, the project becomes complete.

The user receives a completion message summarising:

- project;
- number of milestones completed;
- repository;
- final release/build where applicable;
- any remaining known limitations.

---

# 25. Status Query Workflow

Status queries are independent of the active execution path.

They must not interrupt project processing.

---

## 25.1 Single project query

Examples:

> What's happening with FlowTrack?

> What state is FlowTrack in?

> Where are we with FlowTrack?

Syntra resolves the project name and reads persisted state.

It should respond with:

- project name;
- overall state;
- current milestone;
- milestone objective;
- current activity;
- PR where applicable;
- CI status where applicable;
- latest Architect verdict where applicable;
- active retry/rework details where useful;
- whether human action is required;
- what will happen next.

---

## 25.2 Human-action query

Example:

> Is anything waiting for me?

Syntra should return all projects with unresolved:

- human decisions;
- human tests;
- approvals;
- manual recovery actions.

---

## 25.3 Active-project query

Example:

> What projects are active?

Syntra returns a concise summary of active projects and their current state.

Example:

```text
FlowTrack
M4 — GitHub Actions running
No action required

PulseVault
M2 — Codex implementing dashboard API
No action required

Spread Zeppelin
M6 — Waiting for human UI test
Action required from you
```

---

## 25.4 Detailed status query

A user may request more detail.

Example:

> Give me full status for FlowTrack.

The response may additionally include:

- current branch;
- PR number;
- latest commit;
- current attempt count;
- recent CI results;
- latest review findings;
- milestone start time;
- outstanding risks.

---

# 26. Proactive Notification Workflow

Syntra should proactively message the user for events that matter.

Examples include:

- design ready for approval;
- human decision required;
- human test required;
- repeated failure requires intervention;
- project blocked;
- project completed.

Routine internal events should not generate unnecessary noise.

For example, successful individual commits or every Codex invocation need not result in a message unless configured.

---

# 27. Pause and Resume Workflow

A user should be able to pause a project through the messaging platform.

Example:

> Pause FlowTrack.

Syntra should:

- prevent new workflow work from starting;
- avoid destructive interruption of an active atomic operation;
- record pause state;
- allow safe external tasks already running to finish where appropriate.

The user may later request:

> Resume FlowTrack.

Syntra determines the correct next action from persisted state.

---

# 28. Failure Workflow

Failures are classified according to where they occur.

Examples:

- messaging failure;
- Architect API failure;
- Codex execution failure;
- Git failure;
- GitHub API failure;
- CI failure;
- database failure;
- invalid state;
- policy validation failure.

---

## 28.1 Retryable failure

If the failure is classified as retryable:

- retry count is incremented;
- exponential or configured backoff may apply;
- project retains its workflow position;
- retry occurs automatically.

---

## 28.2 Non-retryable failure

If the failure is not safe to retry:

- the affected workflow pauses;
- failure context is persisted;
- the project enters an intervention state;
- the user is notified where required.

Other projects continue.

---

# 29. Recovery Workflow

On Syntra Build startup, the orchestrator performs recovery.

It should:

1. load all non-complete projects;
2. inspect their persisted workflow state;
3. reconcile local Git workspaces;
4. reconcile GitHub branches and PRs;
5. reconcile active CI runs;
6. identify unfinished AI jobs;
7. identify unresolved human gates;
8. determine the safe next action for each project;
9. resume scheduling.

Recovery should never blindly repeat a privileged action without checking whether it already succeeded.

For example, if Syntra crashes immediately after creating a PR, restart logic must first check whether the PR already exists before attempting to create another.

---

# 30. Multi-Project Scheduling Workflow

Syntra maintains a global work queue.

Each project contributes workflow jobs.

Examples:

- Architect request;
- Codex execution;
- Git operation;
- CI polling;
- status response;
- human gate processing.

---

## 30.1 Resource limits

The scheduler applies configured limits.

Initial expectations may include:

- limited concurrent Codex workers;
- limited concurrent Architect calls;
- serialised privileged repository provisioning;
- lightweight concurrent CI monitoring;
- unlimited waiting human gates.

Exact limits will be defined in `OPERATIONS.md`.

---

## 30.2 Fairness

The scheduler should prevent one repeatedly failing project from consuming all available worker capacity.

A fair queue or round-robin approach should be preferred initially.

---

# 31. Requirements Change During Build

A human may change requirements after implementation has begun.

Example:

> For FlowTrack, I also want diagnostic files automatically compressed.

Syntra should not silently inject that requirement into the active Codex task.

Instead:

1. identify it as a potential scope change;
2. send it to the Architect;
3. determine affected requirements and milestones;
4. update `SPEC.md` if appropriate;
5. record the specification revision;
6. determine whether the current milestone must change;
7. determine whether completed milestones are affected;
8. request human approval if the change is material.

This workflow will be defined in greater detail later.

---

# 32. Cancellation Workflow

The user may cancel a project.

Cancellation must require explicit intent.

Syntra should:

- stop future scheduling;
- preserve project history;
- avoid automatically deleting the GitHub repository;
- close or preserve active PRs according to policy;
- clean up local temporary workspaces safely.

Repository deletion should be treated as a separate privileged action.

---

# 33. Example Happy-Path Workflow

A complete simple milestone may look like this:

```text
User
  "Build project X"

Architect
  design conversation

User
  approves SPEC + AGENTS

Syntra
  creates public GitHub repo
  unless private visibility was explicitly requested

Syntra
  selects M1

Architect
  creates Codex task

Syntra
  creates branch/worktree

Codex
  implements M1
  local tests pass

Syntra
  validates diff

Syntra
  commits

Syntra
  pushes branch

Syntra
  creates PR #1

GitHub Actions
  all checks pass

Architect
  reviews PR #1
  APPROVE

Syntra Gatekeeper
  all gates pass

Syntra
  squash merges PR #1

Syntra
  marks M1 complete

Syntra
  automatically selects M2
```

No human interaction occurs between initial design approval and the next milestone.

---

# 34. Example Review-Rework Workflow

```text
Codex
  implements M3

Syntra
  creates PR #7

GitHub Actions
  PASS

Architect
  CHANGES_REQUIRED
  - empty state missing
  - unit test missing

Syntra
  creates rework task

Codex
  updates same worktree

Syntra
  commits additional changes

Syntra
  pushes same branch

PR #7
  updates automatically

GitHub Actions
  PASS

Architect
  APPROVE

Syntra
  merges PR #7
```

---

# 35. Example Human-Test Workflow

```text
Codex
  implements M5

CI
  PASS

Architect
  HUMAN_TEST_REQUIRED

Syntra → Telegram
  "Please test macOS build..."

User
  reports issue

Architect
  analyses issue

Codex
  fixes issue

CI
  PASS

Architect
  APPROVE

Syntra → Telegram
  requests re-test

User
  PASS

Syntra
  merges
```

---

# 36. Example Concurrent Workflow

At a particular moment, Syntra may report:

```text
FlowTrack
M4
Waiting for GitHub Actions

PulseVault
M2
Codex implementation running

Spread Zeppelin
M5
Waiting for human testing

New Website
DESIGNING
Architect waiting for user response
```

Only the PulseVault Codex worker is consuming significant local execution capacity.

The other projects remain independently resumable.

---

# 37. Workflow Completion Definition

The workflow design will be considered sufficiently defined when:

- every normal project lifecycle step has an owner;
- all privileged operations are mediated by Syntra;
- all human interactions are represented as explicit gates;
- all retry loops are bounded;
- all project progress is recoverable from persisted state;
- status queries can be answered without AI memory;
- multi-project execution does not create ambiguous ownership;
- every milestone has a deterministic path to merge, rework, escalation or failure.

The formal state names and transition rules derived from this workflow will be specified in `STATE_MACHINE.md`.
