#!/usr/bin/env bash
# Idempotent M31 owner/mode normalization. Run only from a reviewed checkout as root.
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
for user in syntra-build syntra-codex; do
  getent passwd "$user" >/dev/null || { echo "missing service identity: $user" >&2; exit 1; }
  shell=$(getent passwd "$user" | cut -d: -f7)
  [[ $shell == /usr/sbin/nologin || $shell == /bin/false ]] || {
    echo "$user must use a non-login shell" >&2; exit 1;
  }
done
[[ $(id -u syntra-build) != "$(id -u syntra-codex)" ]] || {
  echo "service identities must have distinct UIDs" >&2; exit 1;
}
for group in sudo docker syntra-build adm systemd-journal; do
  ! id -nG syntra-codex | tr ' ' '\n' | grep -Fxq "$group" || {
    echo "syntra-codex belongs to privileged group: $group" >&2; exit 1;
  }
done
install -d -o root -g syntra-build -m 0750 /opt/syntra-build /etc/syntra-build
install -d -o syntra-build -g syntra-build -m 0700 \
  /var/lib/syntra-build /var/lib/syntra-build/backups \
  /var/lib/syntra-build/artifacts /var/lib/syntra-build/workspaces \
  /var/lib/syntra-build/repositories /var/log/syntra-build
find /var/lib/syntra-build -maxdepth 1 -type f \
  \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \) \
  -exec chown syntra-build:syntra-build {} + -exec chmod 0600 {} +
if [[ -f /etc/syntra-build/config.json ]]; then
  chown root:syntra-build /etc/syntra-build/config.json
  chmod 0640 /etc/syntra-build/config.json
fi
for secret in telegram-token github-token openai-api-key; do
  if [[ -e /etc/syntra-build/$secret ]]; then
    [[ ! -L /etc/syntra-build/$secret ]] || { echo "$secret must not be a symlink" >&2; exit 1; }
    chown syntra-build:syntra-build "/etc/syntra-build/$secret"
    chmod 0600 "/etc/syntra-build/$secret"
  fi
done
worker_home=$(getent passwd syntra-codex | cut -d: -f6)
chown syntra-codex:syntra-codex "$worker_home"
chmod 0700 "$worker_home"
[[ $(stat -c '%U:%G:%a' /usr/local/libexec/syntra-codex-launch) == root:root:755 ]]
[[ $(stat -c '%U:%G:%a' /etc/sudoers.d/syntra-codex-launch) == root:root:440 ]]
visudo -cf /etc/sudoers.d/syntra-codex-launch >/dev/null
