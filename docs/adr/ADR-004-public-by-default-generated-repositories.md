# ADR-004 — Generated Repositories Are Public by Default

## Status

Accepted

## Context

Syntra Build creates GitHub repositories for generated software projects.

Repository visibility must be deterministic and must not be inferred from source contents, project technology, or an AI assumption.

## Decision

Repositories created by Syntra Build for generated projects are public by default.

A generated project repository is private only when private visibility is explicitly requested by the human and approved during the initial design conversation.

The approved repository visibility is persisted as project state before provisioning.

## Consequences

- Repository provisioning has a simple deterministic default.
- Private visibility is an explicit project requirement.
- Pre-commit and pre-push secret detection is mandatory.
- Detection of sensitive material blocks provisioning or push rather than silently changing repository visibility.
- Private repositories still follow the same secret-handling rules.
