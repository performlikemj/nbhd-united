#!/usr/bin/env bash
# Wrapper around the `claude` binary for OpenClaw's claude-cli backend.
#
# Why this exists:
#
#   OpenClaw's claude-cli backend explicitly clears CLAUDE_CODE_OAUTH_TOKEN
#   from the spawned process's environment (see CLAUDE_CLI_CLEAR_ENV in
#   `extensions/anthropic/cli-shared.js`). Its assumption is that auth
#   lives in `~/.claude/.credentials.json`, written by an interactive
#   `claude auth login` (browser OAuth code exchange that produces a full
#   credential record: accessToken + refreshToken + expiresAt + account
#   info + scopes).
#
#   The BYO flow gives us only what `claude setup-token` prints: a bare
#   long-lived access token. That value works fine as the env var
#   CLAUDE_CODE_OAUTH_TOKEN (verified via `claude --print`), but the
#   `claude` binary refuses to authenticate from a `.credentials.json`
#   that contains only `{accessToken, expiresAt}` — without the OAuth
#   exchange's other fields it returns "Not logged in".
#
#   This wrapper closes the gap by re-injecting the env var that OpenClaw
#   stripped, sourced from the credentials file `entrypoint.sh` wrote
#   from CLAUDE_CODE_OAUTH_TOKEN. Effectively: file → env → claude.
#
#   No-op when the credentials file is absent or the token field is empty,
#   so the wrapper is safe to point at unconditionally — non-BYO tenants
#   won't have the file and the binary just runs as usual.

set -eu

CRED_PATH="${HOME:-/home/node}/.claude/.credentials.json"

if [ -f "$CRED_PATH" ]; then
    TOKEN=$(node -e "
const fs = require('fs');
try {
  const raw = fs.readFileSync(process.argv[1], 'utf8');
  const parsed = JSON.parse(raw);
  process.stdout.write(String(parsed?.claudeAiOauth?.accessToken ?? ''));
} catch {}
" "$CRED_PATH" 2>/dev/null || true)
    if [ -n "${TOKEN:-}" ]; then
        export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"
    fi
fi

exec claude "$@"
