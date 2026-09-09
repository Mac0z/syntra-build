# M16 Architect adapter

M16 introduces a provider-neutral `ArchitectProvider` boundary and an OpenAI
Responses API adapter. The adapter sends a reasoning-only request (no tools) and
uses strict JSON-Schema Structured Outputs. Syntra independently validates the
normalised response, including its project and correlation identities.

Every call is reconstructed from `ProjectDesignContextService`: original request,
ordered messages, active (non-superseded) decisions, current documents, and
explicit open questions. OpenAI session or `previous_response_id` state is not
used for correctness. A `STARTED` request snapshot is committed before network
I/O; the accepted response or classified failure and safe token telemetry are
then persisted. A request left `STARTED` after interruption represents an
ambiguous attempt. Recovery may make another advisory call after inspection;
it must not interpret duplicate responses as accepted decisions.

The OpenAI key crosses only the HTTP authentication boundary. It is excluded
from prompts, snapshots, logs, and user-facing errors. Provider output is
untrusted and has no filesystem, shell, database, GitHub, Telegram, Codex, or
workflow capability. `BLOCKED` is a successful semantic response, not a provider
failure. Timeouts are recorded honestly as potentially ambiguous; throttling,
transient/permanent failures, refusals, incomplete results, malformed
outputs, invalid local input, and configuration failures are distinct classes.
No automatic retry or model escalation is performed in M16.

Architect proposals are advisory. An Architect response does not itself create
an authoritative project decision or workflow transition.

M17 owns SPEC/AGENTS generation; M16 neither creates nor approves those files.
Token counts (including cached input and reasoning tokens when supplied) are
stored without mutable price assumptions.

## Explicit host acceptance

Install `/etc/syntra-build/openai-api-key` owned by the service user with mode
`0600`, enable/configure the `architect` group (provider `openai`, model,
`reasoning_effort`, and `timeout_seconds`), and invoke the dedicated Architect
smoke entry point with an existing disposable project ID. It performs one
persisted logical design request, prints only the normalised response and token
counts, and never advances workflow state. It is deliberately excluded from
`local` smoke and CI. See `python -m syntra_build.architect_smoke --help`.
