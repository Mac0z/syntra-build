# M26 deterministic Gatekeeper and merge

M26 adds one deterministic privileged path from `MERGE_READY` to `COMPLETE`.
The Gatekeeper evaluates persisted project, milestone, repository, pull-request,
CI, Architect, finding, and human-gate evidence against a fresh trusted GitHub
pull-request observation. A separately supplied security-policy seam can add a
known block without granting authority to provider or AI text.

An eligible decision, its individual guard results, a globally serialized merge
attempt, and the `MERGE_READY` → `MERGING` transition are committed before the
GitHub mutation. The mutation includes GitHub's exact-head SHA precondition.
Unknown mutation outcomes are observed rather than replayed. A successful result
only advances to `MERGE_VERIFY`; a separate GitHub GET must prove the exact pull
request is merged before the attempt is finalized and the milestone completes.

Migration 024 is forward-only and creates `merge_eligibility_results` and
`merge_attempts`. Both preserve audit history. Rolling back application code
while retaining schema 024 is not supported by the migration runner; operational
recovery should restore a pre-migration database backup together with the earlier
application revision. No existing table or M1–M25 record is rewritten.

## Bounded host acceptance

Run the real M26 path for an already persisted project, milestone, and PR whose CI,
Architect review, findings, and human gates are already authoritative:

```bash
python -m syntra_build.m26_smoke \
  --database /var/lib/syntra-build/syntra.db \
  --project-id <project-uuid> \
  --milestone-id <milestone-uuid> \
  --pull-request-id <internal-pr-id> \
  --correlation-id <stable-correlation-id> \
  --strategy SQUASH
```

The runner derives repository, PR, branch, and SHA identity from persistence. It
performs one bounded evaluation/prepare/execute/verify sequence and never creates
or alters CI, Architect, finding, or human-gate evidence.
