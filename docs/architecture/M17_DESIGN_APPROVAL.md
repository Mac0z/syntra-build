# M17: specification formalisation and design approval

M17 adds a second, provider-neutral Architect operation. Conversational `design()` remains
small; `draft_specification()` receives a complete reconstructed M15 context and returns a
strictly validated `SpecificationDraft`. OpenAI receives no tools and requests are made with
`store=false`. Each request and accepted response uses the existing Architect audit tables,
including model, reasoning level, correlation, provider response identity, timing, validation
outcome, and token usage.

## Authority and package identity

Syntra resolves repository visibility before the provider call. The default is `public`;
`private` requires a current explicit human design decision. The provider must echo that
value. It cannot make visibility authoritative. Migration 012 defaults existing projects to
public and adds a checked authoritative project column.

A `design_packages` row binds one exact immutable SPEC revision, one exact immutable AGENTS
revision, the audited Architect request, presentation metadata, visibility, and one
`DESIGN_APPROVAL` gate. Cross-project or incorrectly typed draft documents are rejected by a
schema trigger. Full document Markdown remains in the existing document revision store.

## Approval and revision loop

Generation transactionally creates two draft revisions, the package and gate, and moves the
project from `DESIGNING` to `DESIGN_APPROVAL`. Notification happens only after that durable
intent and presents project, visibility, revisions, summary, milestone count, gate ID, and
response syntax.

The package decision operation validates the exact gate/package/project/document correlation.
One SQLite transaction records the response and gate history, approves both documents,
supersedes only previous approved baselines, applies visibility, approves/rejects the package,
and transitions the project. Injected-boundary tests prove rollback. `REQUEST_CHANGES
<feedback>` rejects (but never overwrites) the two drafts, persists feedback, and returns the
project to `DESIGNING`; reconstructed feedback is included in the next draft request.

After restart, package, gate, exact document links, feedback, and approved hashes are read from
SQLite. A notification failure therefore retries notification, not generation.

## M18 boundary and host acceptance

M17 creates no GitHub repository and performs no Git, push, PR, CI, or merge operation. M18
will verify the approved hashes before provisioning.

A deliberately explicit, single paid provider call can be run on the host using the existing
protected secret-file loader:

```text
python -m syntra_build.architect_smoke <project-uuid> \
  --correlation-id <unique-id> --specification-draft
```

This command audits and prints the validated draft but deliberately does not create or approve
a package. Ordinary smoke tests and CI remain offline.

For the real M17 host acceptance flow, the operator explicitly creates (or recovers) the
pending package and notifies its Telegram gate after the package transaction commits:

```text
python -m syntra_build.architect_smoke <project-uuid> \
  --correlation-id <unique-id> --create-package --telegram-chat-id <chat-id>
```

If notification fails, rerunning that exact mode finds the existing pending package and retries
only its `PENDING` gate notification. It does not call the Architect or create document
revisions again. Subsequent `gate <id> REQUEST_CHANGES <feedback>` and `gate <id> APPROVE`
messages are handled by the normal `telegram-once` router and durable Telegram cursor.
