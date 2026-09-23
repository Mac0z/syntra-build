#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "setup-codex-worker.sh must run as root" >&2
  exit 1
fi
id syntra-codex >/dev/null 2>&1 || useradd --system --create-home \
  --shell /usr/sbin/nologin syntra-codex
if getent group syntra-workspaces >/dev/null; then
  gpasswd -d syntra-codex syntra-workspaces >/dev/null 2>&1 || true
  gpasswd -d syntra-build syntra-workspaces >/dev/null 2>&1 || true
fi
for privileged_group in sudo docker; do
  if id -nG syntra-codex | tr ' ' '\n' | grep -Fxq "$privileged_group"; then
    echo "remove syntra-codex from privileged group: $privileged_group" >&2
    exit 1
  fi
done
worker_home=$(getent passwd syntra-codex | cut -d: -f6)
[[ -n $worker_home ]] || { echo "syntra-codex has no configured home" >&2; exit 1; }
install -d -o syntra-codex -g syntra-codex -m 0700 "$worker_home"
install -d -o syntra-codex -g syntra-codex -m 0700 "$worker_home/.config"
command -v setfacl >/dev/null || {
  echo "install the Ubuntu 'acl' package before configuring the Codex worker" >&2
  exit 1
}
install -o root -g root -m 0755 scripts/host/syntra-codex-launch \
  /usr/local/libexec/syntra-codex-launch
install -d -o syntra-build -g syntra-build -m 0700 \
  /var/lib/syntra-build/workspaces
cat >/etc/sudoers.d/syntra-codex-launch <<'EOF'
syntra-build ALL=(root) NOPASSWD: /usr/local/libexec/syntra-codex-launch *
EOF
chmod 0440 /etc/sudoers.d/syntra-codex-launch
visudo -cf /etc/sudoers.d/syntra-codex-launch >/dev/null
