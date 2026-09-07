# ADR-001 — Syntra Owns Workflow State

## Status

Accepted

## Context

Syntra Build coordinates a human project owner, an Architect AI, Codex, Git, GitHub, GitHub Actions, and human approval/testing gates.

If workflow position is stored only in AI conversation state, Telegram history, running processes, or inferred from GitHub, the system cannot reliably recover after a process crash, host restart, provider failure, or network interruption.

Project status also needs to remain queryable independently of the memory of any AI provider.

## Decision

Syntra Build is the authoritative owner of project, milestone, job, and human-gate state.

Material workflow transitions are persisted before the next external side effect begins.

External systems and AI agents produce events and observations, but they do not directly determine authoritative workflow state.

## Consequences

- Project workflow survives application and host restarts.
- Status queries can be answered from durable state.
- AI providers remain replaceable.
- Recovery must reconcile persisted workflow intent with actual external state.
- The persistent state store becomes a critical system dependency.
