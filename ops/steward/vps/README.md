# Steward VPS heartbeat

These are systemd **user** units for the personal OpenClaw VPS. The timer sends
one constant heartbeat every 15 minutes and catches up after downtime with
`Persistent=true`. It does not depend on OpenClaw or its cron engine.

Create the uncommitted credential file first:

```sh
install -d -m 700 ~/.config/steward
umask 077
printf '%s\n' \
  'STEWARD_URL=https://your-control-plane.example' \
  'STEWARD_INGEST_SECRET=replace-with-the-shared-secret' \
  > ~/.config/steward/env
chmod 600 ~/.config/steward/env
```

Then install and start the user units:

```sh
./install.sh
systemctl --user list-timers steward-heartbeat.timer
journalctl --user -u steward-heartbeat.service
```

The sender writes the secret to a mode-600 temporary key file and gives
Python's standard-library `hmac` only that file path. It deliberately does not
use `openssl ... -hmac SECRET`: OpenSSL 3's `mac` CLI also accepts HMAC keys
only as `key:`/`hexkey:` option values, and both forms expose the shared secret
in the process argument list. Ubuntu 24.04 includes the required `python3`,
`curl`, and systemd tools; no additional package is needed.
