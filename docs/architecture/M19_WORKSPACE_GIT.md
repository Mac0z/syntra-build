# M19 — Workspace and trusted Git management

M19 introduces Syntra-owned local Git state beneath the configured data root.
Each verified project has one bare repository at
`repositories/<project-id>/repo.git`; each milestone has one worktree at
`workspaces/<project-id>/<milestone-id>/`. UUID identities and resolved-path
containment checks prevent user text from selecting filesystem locations.

The bare repository is bound durably to the verified M18 GitHub repository,
clean HTTPS `origin`, and `main` default branch. Fetch resolves
`refs/remotes/origin/main` and records its exact SHA. Milestone branches use
`syntra/m<zero-padded-sequence>-<bounded-slug>` and are created from the
workspace's immutable recorded base SHA.

Workspace intent is persisted before branch/worktree creation. Recovery reuses
registered worktrees, reconstructs a missing worktree only from its persisted
repository/branch/base identity, and refuses to adopt an unowned directory.
Wrong remotes or branches fail closed. Dirty files are reported and preserved;
removal refuses dirty worktrees and is idempotent for an already absent path.

Syntra alone stages an explicit path set, commits with `Syntra Build
<syntra@localhost>`, pushes the persisted branch to the same remote branch, and
verifies the remote SHA. Commit evidence is durable. The `commits` table omits
`change_set_id` deliberately: M21 will add validated change-set evidence before
policy-gated commit orchestration exists.

M18 and M19 share one ephemeral askpass facility. Credentials remain outside
URLs, arguments, Git configuration, logs, and SQLite; askpass material is
removed after each operation. M19 does not execute Codex (M20) and performs no
secret or protected-path acceptance checks (M21).
