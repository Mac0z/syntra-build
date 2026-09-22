# M18 repository provisioning

M18 keeps the human-facing `Project.name` unchanged and uses the already-persisted
`Project.canonical_name` as the GitHub repository name. The provider, owner, name,
full name, and approved visibility are persisted as intent before repository creation.
Recovery never re-slugs a display name.

Repository creation is lookup-before-create and reconcile-before-retry. A matching
name is not ownership evidence: an unattributed pre-existing repository blocks the
operation. Once recorded, GitHub's immutable numeric repository ID is the strongest
identity anchor. Ambiguous mutations are inspected before any bounded retry; identity,
visibility, or remote-branch disagreement fails closed. Credentials remain inside
trusted adapters and are never placed in a remote URL or SQLite.

The initial baseline has a dedicated relational record binding the repository and
commit SHA to the exact approved SPEC and AGENTS document IDs, revisions, and SHA-256
hashes. The service independently reads both files at the remote commit and rechecks
repository identity, visibility, default branch, and `main` SHA before atomically
recording verification and transitioning `PROVISIONING` to `READY`.

Only `SPEC.md` and `AGENTS.md` are created by the M18 Git helper. General clones,
worktrees, implementation branches, change-set management, and milestone Git
operations belong to M19 and are deliberately excluded.

## Live acceptance

On a configured host, set `SYNTRA_M18_PROJECT_ID` to a disposable project that is
already in `PROVISIONING` with an approved M17 package, then run
`python -m syntra_build.m18_smoke`. The command performs the real mutation and prints
only the safe project/repository IDs, visibility, baseline SHA, approved hashes,
verification result, and final project state. It never deletes the repository.
