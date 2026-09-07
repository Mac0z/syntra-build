# Syntra Build — Vision

## 1. Purpose

Syntra Build is an AI-assisted software delivery orchestration system designed to take a software project from initial idea through design, implementation, review, testing and completion with minimal manual intervention.

It runs on the Syntra host and coordinates the work of:

- the human project owner;
- an AI software architect;
- an AI coding agent;
- GitHub;
- automated CI/CD systems; and
- the local orchestration services running on Syntra.

Syntra Build is focused specifically on building software products.

It is not intended to be a general-purpose AI orchestration platform.

The system should be capable of building a wide range of software, including:

- desktop applications;
- command-line tools;
- backend services;
- APIs;
- websites;
- web applications;
- multi-tier applications;
- containerised applications; and
- projects using multiple programming languages or technology stacks.

The technology used by an individual project should be defined by that project's requirements rather than by Syntra Build itself.

---

## 2. User Experience

The primary user interface to Syntra Build will be an existing messaging platform accessible from an iPhone.

The user should not need to connect directly to the Syntra host for normal project creation, design, monitoring or status queries.

A typical project will begin with a message such as:

> Create a new project called FlowTrack. I want an application that...

The messaging interface will then be used for:

- discussing requirements;
- designing the application;
- answering architectural questions;
- approving the project specification;
- receiving build progress updates;
- querying the current state of a project;
- resolving important decisions;
- receiving requests for human testing;
- reporting test results;
- responding to failures requiring intervention; and
- confirming project completion.

The messaging system should therefore act as the primary human interface to the development process rather than merely as a notification mechanism.

Telegram is the preferred initial messaging platform.

### Project Status Queries

At any time, the user should be able to ask Syntra Build for the current state of any project using natural language through the messaging platform.

For example:

> What is the current state of FlowTrack?

Syntra Build should respond using its persisted workflow state rather than relying on AI conversation memory.

The response should provide a concise but useful summary including, where applicable:

- project name;
- overall project state;
- current milestone;
- milestone objective;
- current activity;
- active branch;
- active pull request;
- CI status;
- latest Architect review status;
- whether Codex is currently working;
- whether Syntra is waiting for an external system;
- whether human input or testing is required;
- any outstanding failure or retry condition; and
- what is expected to happen next.

A typical response might be:

> FlowTrack is currently on M4 — Diagnostic Capture Optimisation.
>
> Codex has completed the latest revision and PR #12 is running through GitHub Actions.
>
> Windows tests have passed. macOS is still running.
>
> No action is required from you at present.
>
> If CI passes, the PR will move automatically to Architect review.

This status information should be generated from Syntra Build's authoritative project state and supporting execution records.

The user should also eventually be able to ask broader questions such as:

> What projects are currently active?

or:

> Is anything waiting for me?

These queries should allow the messaging platform to act as a lightweight operational console for Syntra Build.

---

## 3. Project Design

Before implementation begins, the human project owner and AI Architect will collaborate to define the project.

The Architect is responsible for understanding the objective, exploring requirements, identifying constraints and proposing a suitable architecture.

This process should remain conversational.

The human project owner should not need to write formal technical specifications manually.

Once the design is sufficiently complete, the Architect will produce the project's two primary source-of-truth documents:

- `SPEC.md`
- `AGENTS.md`

### SPEC.md

`SPEC.md` defines what must be built.

It should include:

- project objectives;
- functional requirements;
- non-functional requirements;
- architecture;
- technology decisions;
- constraints;
- major components;
- project phases or milestones;
- acceptance criteria;
- automated testing requirements;
- human testing requirements where appropriate; and
- the definition of project completion.

### AGENTS.md

`AGENTS.md` defines the engineering rules that coding agents must follow while working on the project.

It may include:

- supported language and runtime versions;
- project structure;
- coding conventions;
- architecture boundaries;
- dependency rules;
- testing requirements;
- build commands;
- security requirements;
- platform compatibility requirements; and
- other durable development instructions.

The project should not enter implementation mode until these documents have been approved.

---

## 4. Project Repositories

Every generated project will have a unique project name.

That name will normally form the basis of its GitHub repository name.

GitHub will be the source-control and CI/CD platform for software projects created by Syntra Build.

The normal repository lifecycle is:

1. project design is completed;
2. `SPEC.md` and `AGENTS.md` are approved;
3. Syntra Build creates the GitHub repository;
4. the approved design documents are committed;
5. repository policies and CI workflows are configured;
6. implementation begins.

Generated applications will therefore use GitHub as their canonical source repository.

The source code for Syntra Build itself will also be stored in GitHub.

This allows the Architect and Codex development workflow to work on Syntra Build using the same repository, pull-request, CI and review mechanisms used for other software projects.

The distinction between Syntra Build and the projects it creates remains architectural rather than being enforced by separate source-control platforms:

- Syntra Build is the orchestration/control-plane application; and
- generated projects are applications managed by that control plane.

Both use GitHub as their canonical source repository.

---

## 5. Core Roles

Syntra Build separates responsibilities between four principal actors.

### Human Project Owner

The human project owner defines intent and retains final authority over product decisions.

The human is responsible for:

- describing the desired outcome;
- answering important requirements questions;
- approving the initial project design;
- making subjective or business decisions that cannot safely be delegated;
- performing human testing where required; and
- reporting problems found during testing.

Routine implementation and review should not require human involvement.

### Architect

The AI Architect acts as the project's software architect and senior technical reviewer.

The Architect is responsible for:

- requirements analysis;
- architectural design;
- producing and maintaining `SPEC.md`;
- producing and maintaining `AGENTS.md`;
- breaking implementation into milestones;
- generating implementation instructions for Codex;
- reviewing implementation pull requests;
- identifying defects or deviations from the specification;
- determining when human input is required; and
- approving completed implementation work.

The Architect does not directly modify GitHub repositories or merge code.

### Codex

Codex acts as the software implementation agent.

Codex is responsible for:

- reading the project specification;
- understanding Architect instructions;
- writing and modifying source code;
- writing and modifying tests;
- running appropriate local tests;
- correcting implementation defects;
- responding to CI failures;
- responding to Architect review feedback; and
- preparing the working tree for submission.

Codex should not hold privileged GitHub credentials.

Codex should not directly:

- create repositories;
- push branches;
- create GitHub pull requests;
- merge pull requests;
- alter branch protection;
- approve its own work; or
- modify Syntra Build's workflow state.

Its output is a change set in a controlled local Git workspace.

### Syntra Build

Syntra Build is the control plane.

It owns the development process and all durable workflow state.

It is responsible for:

- project creation;
- project state;
- scheduling;
- messaging;
- answering project status queries;
- reporting current milestone and activity information;
- AI invocation;
- workspace creation;
- Git branch management;
- validating Codex changes;
- committing changes;
- pushing branches;
- creating GitHub pull requests;
- monitoring CI;
- feeding CI failures back into the development loop;
- sending changes back to Codex when required;
- invoking Architect reviews;
- enforcing human gates;
- merging approved pull requests;
- progressing to subsequent milestones;
- recovering interrupted work; and
- reporting progress to the user.

Neither the Architect nor Codex should be relied upon to remember the state of a project.

Syntra Build is always the authoritative source of workflow state.

---

## 6. Development Workflow

The desired implementation workflow is:

```text
Project idea
    ↓
Design conversation
    ↓
SPEC.md + AGENTS.md
    ↓
Human design approval
    ↓
GitHub repository creation
    ↓
Milestone selected
    ↓
Architect creates Codex task
    ↓
Syntra creates controlled Git workspace
    ↓
Codex implements changes
    ↓
Syntra validates changes
    ↓
Syntra commits and pushes branch
    ↓
Syntra creates GitHub PR
    ↓
GitHub Actions
    ↓
Architect review
    ↓
Approved / Changes required
    ↓
Human test if required
    ↓
Syntra merges
    ↓
Next milestone
    ↓
Project complete
```

The normal path should operate without human intervention once implementation begins.

Human participation should occur only when it adds genuine value.

At any point in this workflow, a user status query should be handled independently of the active processing path and must not disrupt project execution.

---

## 7. Pull Requests

Every implementation milestone should normally pass through a real GitHub pull request.

Codex does not create or submit that pull request directly.

Instead:

1. Syntra creates a dedicated branch or worktree;
2. Codex modifies that workspace;
3. Syntra validates the resulting Git diff;
4. Syntra creates the commit;
5. Syntra pushes the branch to GitHub;
6. Syntra creates the GitHub pull request;
7. GitHub Actions executes;
8. the Architect reviews the PR;
9. Codex addresses any resulting feedback;
10. Syntra updates the existing branch and PR; and
11. Syntra performs the final merge once all required gates have passed.

The same pull request should normally remain open throughout the milestone's review and rework cycle.

This creates a complete and auditable history of each development phase.

---

## 8. Validation and Review

Code should never be merged solely because an AI agent claims that the work is complete.

Before a pull request can be merged, Syntra Build must verify the required gates.

Depending on the project and milestone, these may include:

- expected branch and project identity;
- successful automated tests;
- successful build;
- successful GitHub Actions checks;
- Architect approval;
- no unresolved Architect findings;
- no unresolved critical errors;
- required human testing completed;
- required human decisions completed; and
- milestone acceptance criteria satisfied.

Syntra Build, rather than an AI agent, performs the final decision logic that determines whether a merge is permitted.

---

## 9. Human Gates

The system should distinguish between work that can continue automatically and work requiring human judgement.

Examples of human gates include:

### Product decisions

For example:

- choosing between materially different UI approaches;
- resolving ambiguous business behaviour;
- deciding whether project scope should change.

### Subjective testing

For example:

- visual appearance;
- usability;
- desktop application behaviour;
- mobile layout;
- interaction quality.

### Physical or environmental testing

For example:

- testing a generated Windows executable on a Windows machine;
- testing macOS application behaviour;
- interacting with hardware that Syntra cannot access.

When a human gate is reached, the user should receive a clear message explaining:

- what needs to be decided or tested;
- why it is required;
- what steps to perform where applicable; and
- what response Syntra Build expects.

The affected project should remain safely paused while other projects may continue.

A project status query must clearly identify when a project is waiting at a human gate.

---

## 10. Multi-Project Operation

Syntra Build must support multiple projects concurrently.

A project waiting for:

- GitHub Actions;
- Architect processing;
- human testing;
- human decisions; or
- external services

should not prevent unrelated projects from progressing.

The system must therefore schedule units of work independently.

Concurrency must nevertheless be bounded so that workloads do not exceed the capabilities of the Raspberry Pi Syntra host.

Heavy compilation and multi-platform testing should normally be delegated to GitHub Actions or other external build infrastructure rather than performed on Syntra.

The scheduler should support:

- global concurrency limits;
- per-worker limits;
- project fairness;
- queued work;
- project pausing;
- retry limits; and
- prioritisation if required in future.

Users should be able to query the status of any active project regardless of what other projects are currently executing.

---

## 11. Reliability

Syntra Build must be designed as a durable system rather than a collection of AI conversations.

A restart, crash, loss of connectivity or failed AI request must not lose project state.

At any point Syntra Build should be able to determine:

- which projects exist;
- each project's current state;
- the current milestone;
- the current activity;
- any active branch;
- any active pull request;
- which automated checks have run;
- the latest Architect verdict;
- outstanding human actions;
- previous Codex attempts;
- retry counts; and
- the next valid action.

A Syntra reboot should therefore result in recovery and continuation rather than loss of context.

All important state transitions must be persisted.

The same persisted state must also be sufficient to answer human status queries accurately.

---

## 12. AI Independence

Although the initial implementation will use OpenAI models and Codex, the core orchestration architecture should not depend directly on a particular model.

The system should interact with AI capabilities through defined interfaces such as:

- Architect Provider;
- Coding Agent Provider.

This should make it possible to change:

- model versions;
- reasoning levels;
- API providers;
- coding agents; or
- execution strategies

without redesigning the central workflow engine.

The workflow belongs to Syntra Build, not to the AI provider.

---

## 13. Technology Independence

Syntra Build should not encode assumptions about what kind of software it builds.

Project-specific technology choices belong within the project's specification.

Syntra Build should therefore be capable of orchestrating projects using technologies such as:

- Python;
- Go;
- JavaScript;
- TypeScript;
- React;
- Rust;
- C#;
- Swift;
- Java;
- HTML/CSS;
- SQL;
- Docker;
- combinations of the above; and
- future technologies not known when Syntra Build is implemented.

The orchestration contract should operate primarily on:

- repositories;
- files;
- commands;
- test results;
- build results;
- pull requests; and
- acceptance criteria.

---

## 14. Security and Trust

AI agents must be treated as capable but untrusted workers.

They should operate with the minimum permissions necessary for their role.

In particular:

- Codex should not receive GitHub administrative credentials;
- AI output should be validated before privileged operations;
- GitHub merges should be performed only by Syntra Build;
- repository identity must be verified before destructive operations;
- secrets should not be stored in project source repositories;
- secrets should not be transmitted through Telegram;
- commands executed by agents should operate within controlled workspaces;
- privileged Syntra operations should be separated from agent execution.

The system should prefer deterministic policy checks over asking an AI whether an operation is safe.

---

## 15. Observability

The user should be able to understand what Syntra Build is doing without logging into the host.

Telegram should provide useful project-level status both proactively and on demand.

The user should be able to request the current state of an individual project and receive an accurate summary generated from the orchestration state.

The system should also support broader operational queries such as:

- which projects are active;
- which projects are currently building;
- which projects are waiting for human action;
- which projects have failed;
- which projects are queued; and
- what work Syntra Build is currently performing.

Syntra Build should also maintain sufficient internal telemetry for diagnosis, including:

- structured logs;
- project state transitions;
- AI requests and outcomes;
- Codex execution history;
- CI results;
- GitHub operations;
- retry counts;
- errors;
- execution durations; and
- resource utilisation.

Syntra already hosts Prometheus and Grafana, so Syntra Build should eventually expose appropriate operational metrics to that environment.

Observability must not expose secrets or sensitive credentials.

---

## 16. Simplicity

Syntra Build should remain as simple as reasonably possible.

The initial implementation should favour:

- a small number of components;
- explicit state;
- straightforward interfaces;
- standard protocols;
- durable local persistence; and
- external services for heavyweight work.

The project should not introduce distributed infrastructure, message brokers, databases or microservices unless a demonstrated requirement justifies them.

The initial system should be capable of running comfortably on the existing Syntra Raspberry Pi host.

---

## 17. Core Design Principles

The following principles guide all architectural decisions.

1. **Syntra owns state.**  
   No AI conversation is the source of truth for project progress.

2. **Humans define intent; AI performs most execution.**  
   Human intervention should be reserved for meaningful decisions and testing.

3. **Architect designs and reviews; Codex implements.**  
   The two AI responsibilities remain deliberately separate.

4. **AI agents do not control privileged infrastructure.**  
   Syntra mediates GitHub, repository and workflow operations.

5. **Every significant implementation change is reviewable.**  
   Milestones normally pass through real GitHub pull requests.

6. **Automation must be verifiable.**  
   Tests, CI, acceptance criteria and Architect review determine whether work is acceptable.

7. **Failures are workflow states, not exceptional mysteries.**  
   Retries, rework, escalation and recovery are designed into the system.

8. **Projects are isolated.**  
   One stalled or failing project should not prevent others from progressing.

9. **Technology belongs to the project.**  
   Syntra Build orchestrates development rather than dictating language or framework.

10. **Components should be replaceable.**  
    AI providers and supporting services should sit behind stable interfaces.

11. **Durability comes before autonomy.**  
    An autonomous system that cannot safely recover its state is not acceptable.

12. **Prefer simple mechanisms until complexity is justified.**  
    Syntra Build should remain small enough to understand, operate and maintain.

13. **System state must always be queryable.**  
    A human should be able to ask what any project is doing and receive an accurate answer derived from persisted orchestration state.

---

## 18. Initial Success Criteria

The first production-capable version of Syntra Build should be considered successful when it can:

1. receive a new project request through Telegram;
2. conduct a design conversation with the human owner;
3. generate `SPEC.md` and `AGENTS.md`;
4. obtain explicit design approval;
5. create a new GitHub repository;
6. initialise the repository correctly;
7. identify the first implementation milestone;
8. instruct Codex to implement that milestone;
9. capture Codex's changes in a controlled local Git workspace;
10. commit and push those changes;
11. create a real GitHub pull request;
12. monitor GitHub Actions;
13. automatically return failures to Codex where appropriate;
14. ask the Architect to review the implementation;
15. return review findings to Codex;
16. update the same pull request until approved;
17. request human testing when specified;
18. merge the pull request when all gates are satisfied;
19. move automatically to the next milestone;
20. repeat this process until the project is complete;
21. operate multiple projects concurrently;
22. recover correctly from a Syntra restart during an active project;
23. answer a natural-language status request for any project;
24. report the current milestone, current activity and any outstanding action;
25. identify projects waiting for human intervention; and
26. provide accurate status without relying on AI conversational memory.

A successful demonstration would be the creation of a real application from an initial Telegram request through to a completed GitHub repository without manual Git or GitHub intervention, while allowing the human owner to query its progress at any point through Telegram.

---

## 19. Non-Goals for the Initial Version

The initial version of Syntra Build is not intended to:

- replace GitHub;
- replace GitHub Actions;
- provide its own source-control system;
- provide its own messaging application;
- provide a general-purpose Syntra orchestration platform;
- support arbitrary non-development AI workflows;
- operate as a public multi-user SaaS product;
- execute unlimited parallel coding workloads;
- eliminate all human testing;
- allow AI agents unrestricted access to infrastructure;
- implement sophisticated distributed scheduling;
- provide a custom IDE;
- provide a custom GitHub user interface; or
- optimise for enterprise-scale development organisations.

These capabilities may be reconsidered later if genuine requirements emerge.

---

## 20. Long-Term Direction

Syntra Build should ultimately make creating software feel less like operating development tools and more like managing an autonomous engineering team.

The user should be able to describe the desired outcome, participate in important design decisions, and then observe the system progressing through implementation.

A mature interaction might be as simple as:

> Build me an application that tracks historic FX pricing and lets me compare the market at different points in time.

Followed later by:

> The initial design is ready for approval.

At any point:

> What's happening with that FX project?

And Syntra Build might respond:

> The project is currently on M5 — Historical Data Query Service.
>
> Codex has completed the second implementation attempt. PR #18 is waiting for GitHub Actions to finish.
>
> Three of four checks have passed. No action is required from you.

Then later:

> Milestone 6 passed automated testing but needs you to check the chart interaction on macOS.

And eventually:

> Project complete. All 9 milestones have been merged and the release build is available.

The complexity of Git branches, coding-agent invocation, pull requests, CI runs, retries, architectural reviews and internal project state should be managed by Syntra Build rather than exposed to the user unless intervention is necessary.

That is the fundamental purpose of the system.
