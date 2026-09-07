# ADR-010 — GitHub Actions Handles Heavy and Cross-Platform CI

## Status

Accepted

## Context

The Syntra Raspberry Pi should act primarily as an orchestration and coding-agent host.

Building and testing Windows, macOS, and Linux applications locally would require infrastructure the Pi cannot reasonably provide and would consume resources needed by the control plane.

## Decision

GitHub Actions is the preferred environment for required heavyweight tests, packaging, release builds, and cross-platform CI.

Codex may run useful lightweight local validation, but required CI policy is satisfied by GitHub Actions.

Architect review normally begins only after required CI for the current PR head SHA has passed.

## Consequences

- Windows, macOS, and Linux hosted runners can be used without operating a local build farm.
- The Pi remains focused on orchestration, worktrees, Codex execution, persistence, and messaging.
- CI status must be bound to the exact PR head SHA.
- GitHub Actions availability, quotas, and limits are external operational constraints.
- CI waiting must not consume a Codex worker slot.
