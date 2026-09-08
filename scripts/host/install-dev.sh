#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! $1 =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "usage: install-dev.sh <exact-40-character-git-sha>" >&2
  exit 2
fi
revision=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')

if [[ $(id -u) -eq 0 ]]; then
  echo "run install-dev.sh as the unprivileged syntra-build user" >&2
  exit 1
fi
if [[ $(git status --porcelain) ]]; then
  echo "refusing to install from a dirty source tree" >&2
  exit 1
fi
git cat-file -e "${revision}^{commit}" 2>/dev/null || {
  echo "revision is not available in this checkout" >&2
  exit 1
}
if [[ $(git rev-parse "${revision}^{commit}") != "${revision}" ]]; then
  echo "revision does not resolve to the requested commit" >&2
  exit 1
fi
if [[ $(git rev-parse HEAD) != "${revision}" ]]; then
  echo "HEAD does not equal the requested deployment revision" >&2
  exit 1
fi

python3.14 -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 14))' || {
  echo "python3.14 is absent or is not Python 3.14" >&2
  exit 1
}
python3.14 -m venv /opt/syntra-build/venv
/opt/syntra-build/venv/bin/python -m pip install --upgrade .
printf '%s\n' "${revision}" > /opt/syntra-build/REVISION
printf 'Deployed Syntra revision: %s\n' "$(cat /opt/syntra-build/REVISION)"
