# M21 change validation

M21 treats the M19 worktree—not the Codex result summary—as the authoritative
deliverable. `ChangeCollector` enumerates the base tree, index and untracked
files, then hashes a canonical JSON description containing each path, state,
file kind, mode and exact content SHA-256. Symlink values are hashed without
following them. The resulting `sha256:` value binds append-only validation
evidence to the exact effective tree and staging state.

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

Trusted commit requires an accepted persisted change set and recomputes the
canonical hash immediately before staging. A mismatch refuses the commit,
including modification or removal of an untracked file. Validation performs no
commit, push, pull-request, CI or review operation and preserves rejected
workspaces for diagnosis and rework.

For a disposable M19/M20 acceptance worktree, run validation with
`python -m syntra_build.m21_smoke` and the database, data root, project,
milestone and correlation arguments. Pass the returned hash back using
`--expected-diff-hash` plus explicit `--commit-path` arguments to exercise the
TOCTOU-protected trusted-commit seam. The command never pushes.
