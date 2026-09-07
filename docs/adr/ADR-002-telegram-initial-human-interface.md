# ADR-002 — Telegram Is the Initial Human Interface

## Status

Accepted

## Context

Syntra Build is intended to be operated primarily from an iPhone without requiring routine SSH access or a custom mobile application.

The human must be able to create projects, conduct design conversations, approve specifications, answer decisions, perform human-test gates, query status, and pause or resume projects.

## Decision

Telegram is the initial conversational human interface for Syntra Build.

Telegram is accessed through a messaging adapter so the orchestration domain is not coupled directly to Telegram-specific data structures.

Telegram is a user interface and message transport, not authoritative workflow storage.

## Consequences

- No custom mobile or web application is required for version one.
- Normal project operation can occur from an existing iPhone application.
- Telegram user IDs must be explicitly authorised.
- Important message and gate context must be persisted by Syntra.
- Secrets must not be exchanged through Telegram.
- A different messaging platform can be introduced later through another adapter.
