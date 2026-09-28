#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then echo "install-service.sh requires root" >&2; exit 1; fi
install -d -o syntra-build -g syntra-build -m 0750 /var/lib/syntra-build /var/lib/syntra-build/backups /var/lib/syntra-build/workspaces
install -d -o root -g syntra-build -m 0750 /etc/syntra-build
install -m 0644 deployment/systemd/syntra-build.service /etc/systemd/system/syntra-build.service
systemctl daemon-reload
if [[ ${1:-} == --enable ]]; then systemctl enable syntra-build.service; fi
echo "Unit installed but not started; review config, then start explicitly."
