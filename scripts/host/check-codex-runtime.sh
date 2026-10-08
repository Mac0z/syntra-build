#!/usr/bin/env bash
# No model invocation. Require an operator-verified CLI/tool-host pair.
set -euo pipefail
[[ $# -eq 1 ]] || { echo "usage: check-codex-runtime.sh <approved-codex>" >&2; exit 64; }
case "$1" in /usr/bin/codex|/usr/local/bin/codex) ;; *) exit 65 ;; esac
cli=$1
host=/usr/local/bin/codex-code-mode-host
manifest=/etc/syntra-build/codex-runtime.sha256
for runtime_file in "$cli" "$host" "$manifest"; do
  [[ -f $runtime_file && $(stat -Lc '%u' "$runtime_file") == 0 \
    && $(stat -Lc '%a' "$runtime_file") =~ ^[0-7][0145][0145]$ ]] || {
    echo "Codex execution environment unavailable: root-owned runtime prerequisite missing or writable" >&2
    exit 66
  }
done
[[ -x $cli && -x $host ]] || {
  echo "Codex execution environment unavailable: CLI or Code Mode tool host is not executable" >&2
  exit 66
}
# The root-reviewed manifest must bind exactly the two approved binaries.
[[ $(wc -l < "$manifest") -eq 2 ]] || exit 66
for runtime_file in "$cli" "$host"; do
  expected=$(awk -v path="$runtime_file" '$2 == path {print $1}' "$manifest")
  [[ $expected =~ ^[0-9a-f]{64}$ ]] || exit 66
  actual=$(sha256sum -- "$runtime_file")
  [[ ${actual%% *} == "$expected" ]] || {
    echo "Codex execution environment unavailable: runtime differs from verified pair" >&2
    exit 66
  }
done
# CLI compatibility is checked locally, before ACLs or any paid invocation.
help=$("$cli" exec --help 2>/dev/null)
for option in --sandbox --ignore-user-config --config --enable; do
  [[ $help == *"$option"* ]] || {
    echo "Codex execution environment unavailable: incompatible CLI options" >&2
    exit 66
  }
done
