# M28 status service

M28 is an observational application read path. `SQLiteStatusService` projects a
short-lived, immutable snapshot from committed SQLite records; it does not persist a
derived status record. The projection reads projects, milestones, jobs, workspaces,
pull requests, CI runs/checks, Architect reviews/findings, human gates, M25 feedback
interactions, and blocking/failed transition reasons. Reads are grouped in a SQLite
savepoint and complete before a Telegram response is sent.

## Semantics

Project **state** is the authoritative project lifecycle value. **Activity** describes
the strongest supporting evidence currently present, in this order: active jobs
(`RUNNING`, `DISPATCHED`, `WAITING_EXTERNAL`, `RETRY_WAIT`, then `QUEUED`), outstanding
human actions, persisted activity, milestone state, or no active work. A milestone
state alone never implies that a worker is running.

An **active project** is in `NEW`, `DESIGNING`, `PROVISIONING`, `READY`, `BUILDING`, or
`COMPLETING`. `DESIGN_APPROVAL`, `WAITING_HUMAN`, `PAUSED`, `BLOCKED`, and terminal
projects are excluded because they cannot make implementation progress without a
separate action.

**Waiting for me** consists only of durable human gates in `PENDING` or `NOTIFIED`,
plus active M25 feedback interactions. A gate in `RESPONDED` or `VALIDATED` no longer
requires input and is excluded, as are resolved, expired, and cancelled gates. M25
`PROMPTING` and `WAITING_FEEDBACK` interactions remain tied to the existing human-test
gate and are described as feedback work rather than a new test. Human-test actions
include their exact persisted PR number, tested SHA, CI run, artifact, and instructions
when a binding exists; an expected but absent binding is identified as incomplete.

CI selection is scoped to the selected pull request and prefers its latest attempt for
the exact persisted head; evidence from a historical PR is never borrowed. CI and
Architect evidence is compared with the persisted current pull-request head.
Evidence for an older SHA is displayed as stale and is never represented as current
approval or current passing CI. The service reports the last committed trusted
external observation. M28 deliberately does not add live GitHub reconciliation, so a
GitHub outage cannot prevent status responses.

## Natural-language status intents

After deterministic command parsing returns `UNKNOWN` (never `MALFORMED`), the narrow
resolver accepts only these read-only forms:

- `What projects are active?` / `Which projects are active?`
- `Is anything waiting for me?` / `What is waiting for me?`
- `What's happening with <project>?`
- `What is happening with <project>?`
- `What's the status of <project>?` / `Status of <project>`
- `How is <project> going?`

Extracted project names still use exact canonical-name or stable-ID resolution. The
resolver never emits pause, resume, cancel, create, gate-response, or merge commands.

## Non-interference and host acceptance

Status reads do not claim or create jobs, alter attempts or state, consume worker
capacity, invoke providers, create gates, or initiate CI/Git/GitHub/Gatekeeper work.
No unrestricted Telegram body is logged.

The existing bounded host smoke command is the acceptance seam and uses the actual
host database plus `build_host_router()`:

```bash
python -m syntra_build.smoke telegram-once --config /etc/syntra-build/config.json
```

Send exactly one of `What projects are active?`, `Is anything waiting for me?`, or
`status <exact project name or stable ID>` before the bounded poll. This is not a
daemon and does not implement M30 lifecycle behavior.

No schema migration is required; the schema remains version 25.
