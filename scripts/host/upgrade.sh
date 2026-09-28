#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 || ! $1 =~ ^[0-9a-fA-F]{40}$ ]]; then echo "usage: upgrade.sh <exact-sha>" >&2; exit 2; fi
# The reviewed checkout performs the safety backup before any installed files change.
# This works for the first M29 -> M30 deployment as well as subsequent upgrades.
checkout=$(git rev-parse --show-toplevel)
PYTHONPATH="${checkout}/src" python3.14 -m syntra_build.pre_upgrade \
  --config /etc/syntra-build/config.json
exec scripts/host/install-dev.sh "$1"
