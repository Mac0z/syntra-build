#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "setup-codex-worker.sh must run as root" >&2
  exit 1
fi
getent group syntra-workspaces >/dev/null || groupadd --system syntra-workspaces
id syntra-codex >/dev/null 2>&1 || useradd --system --create-home \
  --shell /usr/sbin/nologin syntra-codex
usermod -a -G syntra-workspaces syntra-codex
usermod -a -G syntra-workspaces syntra-build
install -o root -g root -m 0755 scripts/host/syntra-codex-launch \
  /usr/local/libexec/syntra-codex-launch
install -d -o syntra-build -g syntra-workspaces -m 2770 \
  /var/lib/syntra-build/workspaces
cat >/etc/sudoers.d/syntra-codex-launch <<'EOF'
syntra-build ALL=(root) NOPASSWD: /usr/local/libexec/syntra-codex-launch *
EOF
chmod 0440 /etc/sudoers.d/syntra-codex-launch
visudo -cf /etc/sudoers.d/syntra-codex-launch >/dev/null
