# ADR-003 — GitHub Is the Canonical Source-Control Platform

## Status

Accepted

## Context

Syntra Build requires real pull requests, hosted CI, multi-platform runners, and repository access that supports the same Architect/Codex development process used for applications such as FlowTrack.

An earlier design placed Syntra Build's own source in self-hosted Forgejo while generated projects used GitHub. That would prevent the same GitHub-based Architect and Codex workflow from being used effectively to develop Syntra Build itself.

## Decision

GitHub is the canonical source-control platform for:

- Syntra Build itself; and
- software projects created by Syntra Build.

Syntra Build itself may therefore be developed through the same controlled worktree, pull-request, GitHub Actions, Architect-review, and gated-merge process as other projects.

The distinction between Syntra Build and generated projects is a logical and security boundary, not a source-control-platform boundary.

## Consequences

- The Architect and Codex can participate in development of Syntra Build using the same workflow used for other projects.
- GitHub becomes a core external dependency for source-control operations.
- Syntra Build must preserve strict isolation between its running installation and any Codex worktree used to modify its source.
- Codex still receives no privileged GitHub credentials.
- GitHub repository identities, PRs, SHAs, and CI results must be persisted and reconciled.
