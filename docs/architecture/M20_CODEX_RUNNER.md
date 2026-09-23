# M20 Codex runner

M20 executes the local Codex CLI as an untrusted editor. `CodexRunner` is the
provider-neutral application protocol; `LocalCodexCliRunner` contains CLI and
Linux process details. A `SUCCEEDED` result means only that the process exited
zero. The filesystem remains authoritative and M21, not M20, validates changes.

## Identity and workspace permissions

The control plane runs as `syntra-build`; Codex runs as `syntra-codex`. The
root-owned `syntra-codex-launch` helper accepts only an exact, canonical
`<workspace-root>/<project UUID>/<milestone UUID>` M19 path, one of two fixed
Codex executable paths, and the literal operation `exec -`. It then uses
`setpriv`, initializes the worker's non-privileged groups, sets `no_new_privs`,
and runs Codex. It cannot mediate arbitrary commands. The sudo rule permits only
this validating helper, not a shell or arbitrary root command.

`setup-codex-worker.sh` idempotently creates the system identity, removes legacy
membership from `syntra-workspaces`, rejects `sudo`/`docker` membership, and
keeps the workspace root control-plane-only (`0700`). It requires Ubuntu's
`acl` package. `/etc/syntra-build`, `/opt/syntra-build`, the state database, and
the rest of `/var/lib/syntra-build` retain their existing ownership. Run the
setup script once as root from a reviewed checkout and configure
`codex.executable` to an approved absolute path.

At launch the helper holds a global `flock`. This intentionally serializes the
single `syntra-codex` identity: concurrent ACL grants to the same Unix identity
could not isolate Project A from Project B. Under that lock it removes stale
worker ACLs, grants traversal only through the required parents, `rwX` only on
the exact assigned worktree, default ACLs on its directories for newly created
files, and `rX` only on that project's bare Git metadata. It removes those ACLs
on exit. After forced termination the runner invokes the helper's exact
`--cleanup <validated-worktree>` operation; a later launch also reconciles any
stale ACL before granting access. No shared group grants cross-project access.

## Isolation and lifecycle

Before dispatch, `BoundCodexRunner` asks M19 to re-prove the persisted project,
milestone, repository registration, remote, branch, path containment, and HEAD.
The CLI receives a bounded prompt over stdin and runs with that exact worktree
as its current directory. The helper explicitly connects its original stdin to
the asynchronously supervised Codex child (`<&0`), so `codex exec -` receives
the runner's prompt unchanged while signal forwarding and ACL cleanup remain in
the supervising helper. Its environment is rebuilt from an allowlist:
`PATH`, locale, `TERM`, and `TMPDIR`. The helper then discards that environment
again with `env -i` and sets `HOME` from the `syntra-codex` passwd entry,
`USER=LOGNAME=syntra-codex`, `XDG_CONFIG_HOME=$HOME/.config`, a fixed approved
PATH, locale, `/tmp`, and non-interactive Git safety flags. It does not depend
on sudo environment preservation. GitHub, Telegram, Architect/OpenAI, askpass,
database, and Syntra secret-location variables are therefore not inherited.
Codex discovers its existing ChatGPT/Codex CLI login in the worker home; the
control plane neither reads nor copies it, and M20 has no paid API-key fallback.

Each invocation is persisted as `RUNNING` before launch. Stdout and stderr are
streamed directly to mode-0700 directories at
`<artifact-root>/codex/<run-uuid>/attempt-<n>/{stdout,stderr}.log`; raw provider
output is never copied to normal logs. Completion records safe metadata and a
normalised status. The PID and durable RUNNING record are recovery seams for
M27, which will implement restart reconciliation.

Each process starts a new session. Timeout or cancellation sends `SIGTERM` to
the whole process group, waits a bounded grace period, sends `SIGKILL` if
needed, and reaps the leader. No commit, push, PR, milestone transition, secret
scan, protected-path check, diff hash, or acceptance decision occurs in M20.

## Host smoke

Use the normal runner with explicit existing project, milestone, job, attempt,
and worktree identities and a disposable fake Codex executable installed at an
approved path. Verify `id -un`, `pwd`, a disposable write, denial of a chosen
root-owned secret, absence of secret environment names, captured artifacts,
unchanged `git rev-parse HEAD`, and the persisted result. A second fake command
that spawns `sleep` exercises timeout/process-group termination. This identity
smoke is deterministic and deliberately separate from an optional live Codex
login smoke.

The entry point is `python -m syntra_build.m20_smoke`; it uses the production
`SudoCodexLauncher`, never `DirectProcessLauncher`. `--help` lists its
required database, data root, artifact root, project, milestone, job, attempt,
worktree, executable, and approved `AGENTS.md` inputs. It always passes through
the M19 inspection boundary and the deployed identity helper. The referenced
job and workspace must already exist; the command never provisions or adopts
one. Host acceptance must use a disposable worker executable at an approved
Codex path (or a live CLI in a separate test) that reports `id -un`, `HOME`,
`pwd`, and its environment; writes a marker; attempts the explicitly selected
other-project workspace, `/etc/syntra-build` secret, and SQLite state paths; and
spawns a child for the timeout variant. Record HEAD before and after. Confirm
the other workspace and protected paths are denied, credentials are absent,
the marker and protected artifacts remain, and timeout removes the process
tree. This exercises `syntra-build -> sudo/helper -> syntra-codex` end to end.
