# M6A target-host development deployment

This runbook installs a bounded development checkpoint on the Raspberry Pi 5
named `syntra` (Ubuntu 26.04 LTS, Linux ARM64). It is not a finished production
service.

## Prerequisites and provisioning

Install Git, Python 3.14, and the matching Ubuntu `venv` package. On Ubuntu
26.04 this requires `python3.14-venv`; having the Python interpreter installed
does not guarantee that `ensurepip`/`venv` support is present.

```bash
sudo apt update
sudo apt install git python3.14 python3.14-venv
```

Then verify the exact interpreter rather than relying on `python3`:

```bash
uname -m                         # must report aarch64
python3.14 --version             # must report Python 3.14.x
sudo ./scripts/host/bootstrap-dev.sh
```

The idempotent bootstrap creates the `syntra-build` system account. Code under
`/opt/syntra-build` remains root-owned and read-only to the runtime account;
configuration is root-owned and group-readable; data and logs are owned by the
runtime account. All non-public directories are mode `0750`. Re-running the
script preserves configuration, secrets, and data.

Edit `/etc/syntra-build/config.json` as root and replace the synthetic numeric
Telegram user ID. The file uses the existing typed M1 field names. The default
M1 filesystem and database paths already resolve to the approved `/opt`,
`/etc`, `/var/lib`, and `/var/log` layout, so they need not be duplicated.

Install the token without putting it in an argument or shell history. First
create the destination with its final ownership and permissions, then write the
silently entered token through standard input:

```bash
sudo -n true

sudo -n install \
  -o syntra-build -g syntra-build -m 0600 \
  /dev/null /etc/syntra-build/telegram-token

read -rsp "Telegram bot token: " SYNTRA_TOKEN
printf '\n'

printf '%s\n' "$SYNTRA_TOKEN" | \
  sudo -n tee /etc/syntra-build/telegram-token >/dev/null

unset SYNTRA_TOKEN

sudo stat -c '%U %G %a %n' /etc/syntra-build/telegram-token
```

The expected result is:

```text
syntra-build syntra-build 600 /etc/syntra-build/telegram-token
```

`read -s` disables terminal echo. The token remains in an unexported shell
variable only for the duration of the command and is passed to `tee` over
standard input rather than as a process argument. `sudo -n` prevents sudo from
trying to consume the token stream as a password prompt and fails immediately
if passwordless sudo is unavailable. The destination already has mode `0600`
before the token is written, and `tee` output is discarded so the token is not
printed.

The token file is loaded into the existing `SecretInputs`/`SecretValue` model.
The smoke command rejects group/world-readable token files. Never inspect or
copy this file into logs, Git, or the SQLite database.

## Select and install an exact revision

Clone into a temporary operator-controlled checkout. Fetch `main`, select the
approved merged SHA, verify it is exactly reachable at GitHub's fetched main,
and check out that detached revision. Do not use an unverified `git pull`.

```bash
git clone https://github.com/Mac0z/syntra-build.git /tmp/syntra-build-deploy
cd /tmp/syntra-build-deploy
git fetch origin main
REVISION=$(git rev-parse refs/remotes/origin/main)
git rev-parse "${REVISION}^{commit}"
git checkout --detach "${REVISION}"
test "$(git rev-parse HEAD)" = "${REVISION}"
sudo chown -R syntra-build:syntra-build /opt/syntra-build
sudo -u syntra-build ./scripts/host/install-dev.sh "${REVISION}"
sudo chown -R root:root /opt/syntra-build
sudo chmod 0755 /opt/syntra-build /opt/syntra-build/venv
```

The helper rejects a dirty tree, a non-40-character or unavailable revision,
a HEAD mismatch, root execution, and any runtime other than `python3.14`. It
creates or updates `/opt/syntra-build/venv`, installs the checked-out package
without global pip, and records the SHA in root-protected
`/opt/syntra-build/REVISION`. Persistent data is never removed.

## Local bootstrap and smoke

The local command loads typed configuration and protected secrets, validates
the configured roots, writes structured events to
`/var/log/syntra-build/smoke.log`, opens the configured SQLite database, applies
existing migrations, runs the existing quick integrity check, verifies foreign
keys and WAL, closes the database, and proves M6 routes `ping` to `pong`. It
does not contact Telegram.

```bash
sudo -u syntra-build /opt/syntra-build/venv/bin/python -m syntra_build.smoke local
```

A successful command exits zero and prints only the revision, Python version,
database status, and routing result. A failure exits non-zero with a generic,
secret-safe terminal message.

## One bounded Telegram cycle

Send `/ping` (or `/health` or an unsupported command) from the configured
authorised account, then run:

```bash
sudo -u syntra-build /opt/syntra-build/venv/bin/python \
  -m syntra_build.smoke telegram-once
```

This makes exactly one M5 `getUpdates` poll, preserves provider order, passes
only M5-authorised normalized text messages through the M6 seam, replies using
M5 `send_text`, and exits. `/health` truthfully reports only that local M6A
initialization is available. Project reads/mutations have no M7 implementation.
There is no saved offset or seen-update cache, so a later manual run may see an
update again until durable consumption is implemented. The first bounded poll
may legitimately report `0 authorised updates` if Telegram has not yet surfaced
the newly sent update; rerunning the bounded command is acceptable for this
manual acceptance test.

## Verification, logs, and repeat updates

```bash
cat /opt/syntra-build/REVISION
git -C /tmp/syntra-build-deploy rev-parse HEAD
sudo -u syntra-build tail -n 50 /var/log/syntra-build/smoke.log
```

The two SHA values must equal the approved GitHub `main` SHA. Logs intentionally
exclude token values, credential-bearing URLs, raw provider payloads, and
message bodies. To update, fetch, verify, and detach-checkout a new approved
`main` SHA, then repeat `install-dev.sh` and both smoke commands. Bootstrap is
safe to repeat and neither install nor smoke deletes the database.

Common failures are explicit: correct a Python version error before installing;
run bootstrap if a root is missing; fix ownership if data/log roots are not
writable; restore token mode `0600`; verify the numeric allowlist; and inspect
only the redacted structured log for Telegram/network or SQLite failures.

M6A deliberately provides no `systemd` unit, daemon or polling loop, persistent
Telegram consumption/deduplication, scheduler, M7 state transitions,
Architect/Codex/GitHub orchestration, metrics service, production upgrades,
self-hosting, or GitHub automatic deployment. The real Pi/Telegram acceptance
test occurs manually after merge.
