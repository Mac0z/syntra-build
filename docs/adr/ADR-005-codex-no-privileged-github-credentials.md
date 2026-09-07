# ADR-005 — Codex Receives No Privileged GitHub Credentials

## Status

Accepted

## Context

Codex must inspect, modify, and test project source code. Giving the coding agent direct GitHub write or administrative authority would allow an AI-controlled execution process to bypass Syntra's deterministic control plane.

This applies equally when Codex is working on Syntra Build itself.

## Decision

Codex receives no privileged GitHub credentials.

Codex may:

- inspect and modify files in its assigned worktree;
- create and remove project files as required;
- run permitted local tests, linters, and build commands;
- inspect local Git status and diffs.

Codex may not directly:

- push branches;
- create repositories;
- create pull requests;
- merge pull requests;
- alter branch protection;
- change repository administration;
- modify Syntra workflow state.

## Consequences

- A mistaken or compromised Codex run cannot directly administer or merge GitHub repositories.
- Codex must run under a restricted execution identity.
- Syntra must validate Codex's worktree before accepting changes.
- Git and GitHub side effects are performed by trusted Syntra components.
