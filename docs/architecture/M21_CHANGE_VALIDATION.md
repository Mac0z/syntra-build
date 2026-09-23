# M21 change validation

M21 treats the M19 worktree—not the Codex result summary—as the authoritative
deliverable. `ChangeCollector` enumerates the base tree, index and untracked
files, then hashes a canonical JSON description containing each path, state,
file kind, mode and exact content SHA-256. Symlink values are hashed without
following them. The resulting `sha256:` value binds append-only validation
evidence to the exact effective tree and staging state.

`base_sha` remains the immutable original milestone branch point. In contrast,
`trusted_head_sha` is the latest Syntra-authoritative commit on that branch and
is the comparison tree for collection and hashing. They are equal for the first
implementation cycle; after a trusted commit, later rework is collected only
relative to the new `trusted_head_sha`, so already committed unchanged files do
not reappear.

Validation independently checks the persisted repository, workspace path,
worktree registration, common Git directory, remote, branch and trusted HEAD.
Identity, history and workspace-escape findings block. Empty changes,
unauthorised protected paths and likely credentials require rework. Explicitly
authorised protected changes are retained as informational audit findings.

The initial offline scanner is a replaceable conservative regular-expression
scanner for provider tokens, private-key headers, cloud keys, credential-bearing
URLs and obvious secret assignments. Findings persist only the rule, safe line
location and a truncated non-reversible SHA-256 fingerprint—never matching
secret text. Binary content is exact-byte hash-bound and reported as unscanned.

Regular text is read once by `ChangeCollector`. The exact byte object used for
its content hash is retained in a process-local scan snapshot and passed to the
secret scanner; validation never re-reads the mutable path. Snapshot bytes are
neither persisted nor logged and live only for one validation call. Retention is
bounded to 2 MiB per file and 8 MiB per validation. A text file outside that
policy produces an explicit blocking `TEXT_SCAN_LIMIT_EXCEEDED` finding rather
than being silently skipped.

Trusted commit requires an accepted persisted change set and recomputes the
canonical hash against the current persisted trusted HEAD immediately before
staging. ACCEPT evidence must match the workspace, trusted HEAD, and diff hash;
the resulting commit immutably records that exact change-set ID and hash.

Trusted staging then stages exactly the accepted path set. Syntra describes the
resulting index and compares each path's status, Git mode, file kind, binary
classification, and exact SHA-256 content identity with persisted ChangeSet
evidence. The Codex-observed `staged` flag remains audit metadata and is not part
of this comparison: the trusted staging operation intentionally stages the full
accepted ChangeSet. A mismatch refuses the commit,
including modification or removal of an untracked file. Validation performs no
commit, push, pull-request, CI or review operation and preserves rejected
workspaces for diagnosis and rework.

Once the index matches, `git commit` consumes that verified immutable input.
Working-tree mutation after staging cannot change the committed bytes. Syntra
checks for residual changes after commit and records the workspace as `DIRTY`
rather than discarding them; a clean post-commit workspace becomes `READY`.

For a disposable M19/M20 acceptance worktree, run validation with
`python -m syntra_build.m21_smoke` and the database, data root, project,
milestone and correlation arguments. Pass the returned hash back using
`--expected-diff-hash` plus explicit `--commit-path` arguments to exercise the
TOCTOU-protected trusted-commit seam. The command never pushes.
