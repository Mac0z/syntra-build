#!/usr/bin/env bash
# Run under the worker identity, after temporary ACL grants and before Codex.
set -euo pipefail
[[ $# -eq 2 ]] || exit 64
[[ -r $1/SPEC.md && -r $1/AGENTS.md ]] || {
  echo "Codex execution environment unavailable: approved documents are unreadable" >&2
  exit 66
}
probe=$(mktemp "$1/.syntra-write-check.XXXXXXXX") || {
  echo "Codex execution environment unavailable: effective worktree is read-only" >&2
  exit 66
}
rm -- "$probe"
[[ ! -w $2 && ! -w $2/HEAD ]] || {
  echo "Codex execution environment unavailable: protected Git metadata is writable" >&2
  exit 66
}
