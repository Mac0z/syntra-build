# ADR-006 — Syntra Controls Commits, Pushes, Pull Requests, and Merges

## Status

Accepted

## Context

Codex's reliable output is a changed local worktree.

Repository operations must remain deterministic, auditable, recoverable, and bound to the exact project, branch, milestone, and commit SHA.

## Decision

Syntra Build owns accepted Git and GitHub mutations.

For a milestone, Syntra:

1. creates or prepares the controlled branch and worktree;
2. invokes Codex;
3. inspects and validates the resulting change set;
4. stages and creates the accepted commit;
5. pushes the branch;
6. creates or reuses the milestone pull request;
7. monitors required CI;
8. coordinates Architect review and rework;
9. performs deterministic pre-merge Gatekeeper checks;
10. merges the pull request.

Rework uses the same branch and pull request for the milestone.

## Consequences

- Every accepted change passes through Syntra validation.
- Git history and PR ownership remain deterministic.
- A single real PR represents each milestone's implementation/rework cycle.
- Restart recovery can reconcile branch, PR, CI, and merge state.
- Codex cannot approve or submit its own work independently.
