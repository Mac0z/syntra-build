# ADR-009 — Use a Single Service Before Microservices

## Status

Accepted

## Context

Syntra Build contains many logical components, including messaging, orchestration, scheduling, state management, recovery, Architect integration, Codex execution, Git, GitHub, CI monitoring, and status reporting.

The initial workload does not justify the deployment and failure complexity of a distributed architecture.

## Decision

Version one of Syntra Build will run as a single long-running Python service with clear internal module and adapter boundaries.

The initial architecture does not require:

- Redis;
- RabbitMQ;
- Celery;
- Kubernetes;
- internal HTTP microservices;
- distributed worker coordination.

Components communicate through typed internal interfaces, domain events, persisted jobs, and state-machine transitions.

## Consequences

- Deployment and debugging remain simple.
- Resource overhead is appropriate for the Raspberry Pi.
- Transactions and recovery are easier to reason about.
- Internal boundaries must remain clean enough to permit future extraction of workers or services if scale requires it.
