#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 || ! $1 =~ ^[0-9a-fA-F]{40}$ ]]; then echo "usage: upgrade.sh <exact-sha>" >&2; exit 2; fi
admin=/opt/syntra-build/venv/bin/syntra-build-admin
if [[ ! -x $admin ]]; then echo "M30 CLI absent: perform and verify the documented bootstrap backup first" >&2; exit 1; fi
"$admin" backup --reason pre-upgrade
"$admin" integrity
exec scripts/host/install-dev.sh "$1"
