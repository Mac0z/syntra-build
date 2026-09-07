# ADR-007 — SQLite Is the Initial Persistent State Store

## Status

Accepted

## Context

Syntra Build needs durable transactional workflow state, audit history, job and gate tracking, recovery metadata, and efficient project status queries.

The initial system is a single service running on a Raspberry Pi with modest concurrency.

## Decision

SQLite is the initial persistent state store for Syntra Build.

The implementation will use:

- foreign-key enforcement;
- WAL mode;
- explicit transactions;
- explicit schema migrations;
- database-safe backup procedures;
- integrity checks before autonomous recovery.

## Consequences

- No separate database server is required.
- Deployment and backup remain simple.
- SQLite is suitable for the expected initial project and event volume.
- Asynchronous code must still guard against stale writes.
- The persistence abstraction should allow migration to PostgreSQL later if scale or distributed execution requires it.
