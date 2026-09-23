# M20 Codex runner

M20 executes the local Codex CLI as an untrusted editor. `CodexRunner` is the
provider-neutral application protocol; `LocalCodexCliRunner` contains CLI and
Linux process details. A `SUCCEEDED` result means only that the process exited
zero. The filesystem remains authoritative and M21, not M20, validates changes.

## Identity and workspace permissions

The control plane runs as `syntra-build`; Codex runs as `syntra-codex`. The
root-owned `syntra-codex-launch` helper accepts only a canonical existing path
under `/var/lib/syntra-build/workspaces` and one of two fixed Codex executable
paths. It then uses `setpriv`, initializes the worker's non-privileged groups,
sets `no_new_privs`, and execs Codex. The sudo rule permits only this validating
helper, not a shell or arbitrary root command.

`setup-codex-worker.sh` idempotently creates the system identity and a dedicated
`syntra-workspaces` group. Only the workspace root is group-accessible (setgid,
`2770`); `/etc/syntra-build`, `/opt/syntra-build`, the state database, and the
rest of `/var/lib/syntra-build` retain their existing ownership. Deployments
must run the control-plane service with `UMask=0007` so newly created worktrees
remain writable by that group. The worker must not be added to `sudo`, `docker`,
or other privileged groups. Run the setup script once as root from a reviewed
checkout and configure `codex.executable` to an approved absolute path.

## Isolation and lifecycle

Before dispatch, `BoundCodexRunner` asks M19 to re-prove the persisted project,
milestone, repository registration, remote, branch, path containment, and HEAD.
The CLI receives a bounded prompt over stdin and runs with that exact worktree
as its current directory. Its environment is rebuilt from an allowlist:
`HOME`, `PATH`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`, and `XDG_CONFIG_HOME`, plus
non-interactive Git safety flags. GitHub, Telegram, Architect/OpenAI, askpass,
database, and Syntra secret-location variables are therefore not inherited.
`HOME`/`XDG_CONFIG_HOME` point at the worker's existing Codex/ChatGPT CLI login;
M20 has no paid API-key fallback.

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

The entry point is `python -m syntra_build.m20_smoke`; `--help` lists its
required database, data root, artifact root, project, milestone, job, attempt,
worktree, executable, and approved `AGENTS.md` inputs. It always passes through
the M19 inspection boundary and the deployed identity helper. The referenced
job and workspace must already exist; the command never provisions or adopts
one. Record HEAD before and after the command when performing host acceptance.
