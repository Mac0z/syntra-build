# ADR-008 — Syntra Build Uses Python 3.14

## Status

Accepted

## Context

The orchestration service needs mature support for asynchronous I/O, subprocess management, SQLite, HTTP APIs, structured validation, Telegram, Git/GitHub integration, testing, and straightforward ARM64 deployment on the Syntra Raspberry Pi.

Python 3.12 was previously selected because of compatibility requirements associated with FlowTrack and an older macOS target. Those constraints do not apply to Syntra Build, which will run on the Syntra Raspberry Pi host.

## Decision

Syntra Build will use Python 3.14 as its supported runtime baseline.

The supported package range will initially be:

```text
>=3.14,<3.15
```

Development, CI, and deployment will use the Python 3.14 feature series rather than pinning to a specific patch release.

Patch-level updates within Python 3.14 are expected to be adopted normally.

## Consequences

- Syntra Build can take advantage of a newer Python runtime without inheriting FlowTrack's legacy platform constraint.
- Development and CI must test against Python 3.14.
- Dependencies used on Syntra must support Python 3.14 and Linux ARM64.
- Package metadata should declare `>=3.14,<3.15`.
- A future move to Python 3.15 or later will require an explicit compatibility decision rather than accidental runtime drift.
