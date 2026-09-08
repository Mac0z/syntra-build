#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "bootstrap-dev.sh must be run explicitly as root" >&2
  exit 1
fi

if [[ $(uname -s) != Linux || $(uname -m) != aarch64 ]]; then
  echo "warning: M6A targets Linux ARM64; detected $(uname -s) $(uname -m)" >&2
fi

if ! id syntra-build >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/syntra-build --shell /usr/sbin/nologin syntra-build
fi

install -d -o root -g root -m 0755 /opt/syntra-build
install -d -o root -g syntra-build -m 0750 /etc/syntra-build
install -d -o syntra-build -g syntra-build -m 0750 \
  /var/lib/syntra-build /var/lib/syntra-build/workspaces \
  /var/lib/syntra-build/backups /var/log/syntra-build

config=/etc/syntra-build/config.json
if [[ ! -e ${config} ]]; then
  install -o root -g syntra-build -m 0640 /dev/stdin "${config}" <<'EOF'
{
  "telegram": {
    "enabled": true,
    "authorised_user_ids": [123456789],
    "polling_timeout_seconds": 15
  },
  "logging": {"level": "INFO", "structured": true},
  "metrics": {"enabled": false},
  "backups": {"enabled": false}
}
EOF
  echo "Created ${config}; replace the placeholder Telegram user ID."
else
  echo "Preserved existing ${config}."
fi

echo "Host development directories are ready; no secret was created or changed."
