#!/usr/bin/env bash
set -eu

OPENCLAW_HOME="${OPENCLAW_HOME:-/home/node/.openclaw}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-$OPENCLAW_HOME/openclaw.json}"
OPENCLAW_WORKSPACE_PATH="${OPENCLAW_WORKSPACE_PATH:-$OPENCLAW_HOME/workspace}"
NBHD_MANAGED_SKILLS_SRC="${NBHD_MANAGED_SKILLS_SRC:-/opt/nbhd/agent-skills}"
NBHD_MANAGED_SKILLS_DST="${NBHD_MANAGED_SKILLS_DST:-$OPENCLAW_WORKSPACE_PATH/skills/nbhd-managed}"
NBHD_MANAGED_AGENTS_TEMPLATE="${NBHD_MANAGED_AGENTS_TEMPLATE:-/opt/nbhd/templates/openclaw/AGENTS.md}"
NBHD_MANAGED_AGENTS_DST="${NBHD_MANAGED_AGENTS_DST:-$OPENCLAW_WORKSPACE_PATH/AGENTS.md}"
NBHD_MEMORY_DIR="${NBHD_MEMORY_DIR:-$OPENCLAW_WORKSPACE_PATH/memory}"

mkdir -p "$OPENCLAW_HOME" "$OPENCLAW_WORKSPACE_PATH" "$NBHD_MEMORY_DIR"

# Skill templates.md is tenant-specific and authoritative on the file share
# (rewritten by Django's update_tenant_config on every default-template edit).
# It lives *inside* the nbhd-managed skills tree, so the rm -rf below would
# wipe it and revert to a stale boot-time copy. Preserve the share copy across
# the image-default refresh: stash it before the wipe, restore it after.
SKILL_TEMPLATES_DST="${NBHD_MANAGED_SKILLS_DST}/daily-journal/references/templates.md"
SKILL_TEMPLATES_STASH=""
if [ -d "$NBHD_MANAGED_SKILLS_SRC" ]; then
    if [ -f "$SKILL_TEMPLATES_DST" ]; then
        SKILL_TEMPLATES_STASH="$(mktemp)"
        cp "$SKILL_TEMPLATES_DST" "$SKILL_TEMPLATES_STASH"
    fi
    rm -rf "$NBHD_MANAGED_SKILLS_DST"
    mkdir -p "$NBHD_MANAGED_SKILLS_DST"
    cp -R "$NBHD_MANAGED_SKILLS_SRC"/. "$NBHD_MANAGED_SKILLS_DST"/
    if [ -n "$SKILL_TEMPLATES_STASH" ]; then
        mkdir -p "$(dirname "$SKILL_TEMPLATES_DST")"
        cp "$SKILL_TEMPLATES_STASH" "$SKILL_TEMPLATES_DST"
        rm -f "$SKILL_TEMPLATES_STASH"
    fi
fi

# AGENTS.md — SEED-ONCE, then the file share is authoritative.
# NBHD_AGENTS_MD is a provision-time snapshot that goes STALE the instant Django
# re-renders persona / per-tenant gates / Gravity and writes the fresh copy to
# the share (update_tenant_config -> upload_workspace_file 'workspace/AGENTS.md',
# and the container-started hook re-asserts it on every boot). The old
# always-overwrite-from-env reverted EVERY restart back to that stale snapshot,
# silently dropping persona + gate changes. So seed only when the share has no
# usable copy (first boot, or a 0-byte file from an interrupted write) and never
# clobber a real share copy. System-control is preserved: Django overwrites the
# share on every config-apply AND on every boot via the container-started hook,
# so a tenant/agent can't permanently corrupt AGENTS.md. Mirrors the seed-once
# guards used for skill-templates.md and SOUL.md/IDENTITY.md below.
if [ ! -s "$NBHD_MANAGED_AGENTS_DST" ]; then
    if [ -n "${NBHD_AGENTS_MD:-}" ]; then
        printf '%s\n' "$NBHD_AGENTS_MD" > "$NBHD_MANAGED_AGENTS_DST"
    elif [ -f "$NBHD_MANAGED_AGENTS_TEMPLATE" ]; then
        cp "$NBHD_MANAGED_AGENTS_TEMPLATE" "$NBHD_MANAGED_AGENTS_DST"
    fi
fi

# Skill templates.md — seed from env var ONLY when the file share has no copy
# (first boot, or a tenant that predates the share-write path). The
# NBHD_SKILL_TEMPLATES_MD env var is a provision-time snapshot that goes stale
# after the user edits their default template; Django's update_tenant_config
# writes the authoritative copy to the share (preserved across the rm -rf
# above), so we must NOT clobber an existing share copy with the stale env var.
if [ -n "${NBHD_SKILL_TEMPLATES_MD:-}" ] && [ -z "$SKILL_TEMPLATES_STASH" ]; then
    if [ -d "$(dirname "$SKILL_TEMPLATES_DST")" ]; then
        printf '%s\n' "$NBHD_SKILL_TEMPLATES_MD" > "$SKILL_TEMPLATES_DST"
    fi
fi

# SOUL.md, IDENTITY.md — seed once from env var, don't overwrite
if [ -n "${NBHD_SOUL_MD:-}" ] && [ ! -f "$OPENCLAW_WORKSPACE_PATH/SOUL.md" ]; then
    printf '%s\n' "$NBHD_SOUL_MD" > "$OPENCLAW_WORKSPACE_PATH/SOUL.md"
fi
if [ -n "${NBHD_IDENTITY_MD:-}" ] && [ ! -f "$OPENCLAW_WORKSPACE_PATH/IDENTITY.md" ]; then
    printf '%s\n' "$NBHD_IDENTITY_MD" > "$OPENCLAW_WORKSPACE_PATH/IDENTITY.md"
fi

# USER.md, TOOLS.md — seed from static templates if missing
NBHD_TEMPLATES_DIR="${NBHD_TEMPLATES_DIR:-/opt/nbhd/templates/openclaw}"
for file in USER.md TOOLS.md MEMORY.md HEARTBEAT.md; do
    src="$NBHD_TEMPLATES_DIR/$file"
    dst="$OPENCLAW_WORKSPACE_PATH/$file"
    if [ -f "$src" ] && [ ! -f "$dst" ]; then
        cp "$src" "$dst"
    fi
done

# Only write OPENCLAW_CONFIG_JSON if the config file doesn't already exist
# (file share mount is the source of truth after first boot)
if [ ! -f "$OPENCLAW_CONFIG_PATH" ] && [ -n "${OPENCLAW_CONFIG_JSON:-}" ]; then
    printf '%s\n' "$OPENCLAW_CONFIG_JSON" > "$OPENCLAW_CONFIG_PATH"
fi

# Note: No webhook secret injection needed — channels.telegram is absent.
# The central Django poller authenticates via gateway token (Bearer auth).

# Validate config exists and contains valid JSON.
# Retry up to 30s to handle in-flight config writes from Django.
MAX_CONFIG_RETRIES=6
CONFIG_RETRY_DELAY=5
for _attempt in $(seq 1 $MAX_CONFIG_RETRIES); do
    if [ -f "$OPENCLAW_CONFIG_PATH" ] && node -e "JSON.parse(require('fs').readFileSync(process.argv[1]))" "$OPENCLAW_CONFIG_PATH" 2>/dev/null; then
        break
    fi
    if [ "$_attempt" -eq "$MAX_CONFIG_RETRIES" ]; then
        echo "Config file missing or invalid after ${MAX_CONFIG_RETRIES} retries at $OPENCLAW_CONFIG_PATH" >&2
        exit 1
    fi
    echo "Config not ready (attempt $_attempt/$MAX_CONFIG_RETRIES), retrying in ${CONFIG_RETRY_DELAY}s..." >&2
    sleep $CONFIG_RETRY_DELAY
done

# --- BYO Anthropic Claude CLI: bootstrap auth state from env var ---
#
# When `CLAUDE_CODE_OAUTH_TOKEN` is bound (by Django via
# `apply_byo_credentials_to_container`), seed the Claude CLI's local
# credentials file and register the OpenClaw `anthropic:claude-cli` auth
# profile. The auth profile (NOT a model-prefix shape like
# `anthropic-cli/...`) is what makes OpenClaw route `anthropic/<model>`
# requests through the bundled `claude` binary against the tenant's
# Pro/Max subscription. Skipped silently when the env var is absent.
#
# `openclaw models auth login` is idempotent: re-running on subsequent
# boots refreshes the profile if already present. The command checks
# `process.stdin.isTTY` (auth-BQuNQ6PP.js:362) and exits non-zero when
# false — fixed here by wrapping it in `script(1)` (from `bsdmainutils`)
# which provides a pty.
#
# The `--set-default` flag is intentionally OMITTED so we don't clobber
# the per-tenant `agents.defaults.model.primary` Django writes from
# `tenant.preferred_model`. Auth profile registration alone is enough to
# enable CLI routing.
#
# Persistence: ~/.claude/projects/ stores claude's per-conversation
# session JSONL files. By default it lives on the container's writable
# layer and is wiped on every revision bump (deploy, config change,
# hibernation wake). We symlink it onto /home/node/.openclaw/ (which IS
# the file share mount — see `apps/orchestrator/azure_client.py`'s
# `volumeMounts`) so conversation context survives container restarts.
# The auth profile itself sits under ~/.openclaw/agents/ which is an
# EmptyDir overlay — that's intentional (PR #387, chmod EPERM mitigation)
# but it means we MUST re-register on every boot.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    CLAUDE_CRED_DIR="$HOME/.claude"
    CLAUDE_CRED_PATH="$CLAUDE_CRED_DIR/.credentials.json"
    mkdir -p "$CLAUDE_CRED_DIR"
    # Far-future expiresAt — `claude setup-token` issues long-lived tokens,
    # but OpenClaw's parser still requires a positive finite ms timestamp.
    BYO_EXPIRES_AT=$(node -e "process.stdout.write(String(Date.now() + 10*365*24*60*60*1000))")
    umask 077
    printf '{"claudeAiOauth":{"accessToken":"%s","expiresAt":%s}}\n' \
        "$CLAUDE_CODE_OAUTH_TOKEN" "$BYO_EXPIRES_AT" > "$CLAUDE_CRED_PATH"
    chmod 600 "$CLAUDE_CRED_PATH"
    echo "[entrypoint] wrote $CLAUDE_CRED_PATH (BYO Anthropic CLI)"

    # Tool-deny policy. The BYO Claude CLI runs as a model backend for
    # OpenClaw, NOT as the tool-execution layer — OpenClaw owns tools via
    # its own `tools.allow/deny` policy. But Claude CLI ships with its own
    # native Bash/Edit/Write/WebFetch tools that fire if the model output
    # produces tool calls; a prompt-injected assistant can use those to
    # `printenv` (Bash), exfil via web_fetch, or read /proc/self/environ
    # (Read). Locking these down at ~/.claude/settings.json applies to
    # every claude invocation in this container regardless of CLI args.
    # See runtime/openclaw/claude-settings.json for the deny list.
    if [ -f /opt/nbhd/claude-settings.json ]; then
        cp /opt/nbhd/claude-settings.json "$CLAUDE_CRED_DIR/settings.json"
        chmod 600 "$CLAUDE_CRED_DIR/settings.json"
        echo "[entrypoint] wrote $CLAUDE_CRED_DIR/settings.json (BYO tool deny policy)"
    fi

    # Persist claude session JSONLs by symlinking ~/.claude/projects to
    # the file share. Idempotent: only creates the link when it isn't
    # already there. If a real (non-symlink) projects dir exists from a
    # prior boot AND it's empty, replace with the symlink; if non-empty,
    # leave it (rare — would imply someone wrote without our setup).
    CLAUDE_PROJECTS_PERSISTENT="$OPENCLAW_HOME/claude-state/projects"
    mkdir -p "$CLAUDE_PROJECTS_PERSISTENT"
    if [ ! -L "$CLAUDE_CRED_DIR/projects" ]; then
        if [ -d "$CLAUDE_CRED_DIR/projects" ] && [ -z "$(ls -A "$CLAUDE_CRED_DIR/projects" 2>/dev/null)" ]; then
            rmdir "$CLAUDE_CRED_DIR/projects" 2>/dev/null || true
        fi
        if [ ! -e "$CLAUDE_CRED_DIR/projects" ]; then
            ln -s "$CLAUDE_PROJECTS_PERSISTENT" "$CLAUDE_CRED_DIR/projects"
            echo "[entrypoint] symlinked $CLAUDE_CRED_DIR/projects -> $CLAUDE_PROJECTS_PERSISTENT"
        fi
    fi

    # Register OpenClaw auth profile. `script -qfec` runs the command
    # inside a pty so its TTY check passes. Output captured to /tmp for
    # debug; result swallowed because the auth profile is non-critical
    # for non-Anthropic routing (other providers keep working without it).
    if command -v script >/dev/null 2>&1; then
        if script -qfec "openclaw models auth login --provider anthropic --method cli" /tmp/openclaw-auth-login.log >/dev/null 2>&1; then
            echo "[entrypoint] registered OpenClaw auth profile anthropic:claude-cli (via script-pty)"
        else
            echo "[entrypoint] openclaw models auth login (script-pty) failed; tail of /tmp/openclaw-auth-login.log:" >&2
            tail -n 5 /tmp/openclaw-auth-login.log 2>/dev/null >&2 || true
        fi
    else
        echo "[entrypoint] script(1) not installed; cannot register OpenClaw auth profile non-interactively" >&2
    fi
fi

# --- Dual-process supervisor: OpenClaw gateway + reverse proxy ---

if [ "$#" -gt 0 ]; then
    case "$1" in
        gateway)
            shift
            GATEWAY_ARGS="$*"
            ;;
        -*)
            GATEWAY_ARGS="$*"
            ;;
        *)
            # Non-gateway subcommand — run directly (no proxy needed)
            exec openclaw "$@"
            ;;
    esac
else
    GATEWAY_ARGS=""
fi

# Unset TELEGRAM_BOT_TOKEN so OpenClaw does NOT start a Telegram provider.
# The central Django poller handles all inbound Telegram messages and
# forwards them to this container via /v1/chat/completions.
unset TELEGRAM_BOT_TOKEN

# Start both processes in background
# shellcheck disable=SC2086
openclaw gateway --allow-unconfigured $GATEWAY_ARGS &
GATEWAY_PID=$!

node /opt/nbhd/proxy.js &
PROXY_PID=$!

# Container-started hook — fire-and-forget POST to Django so the
# postgres-canonical reconciler can rebuild SQLite from Postgres truth
# the moment we're ready, instead of waiting for the hourly fleet
# reconcile. Quietly skipped if env vars are missing.
(
    if [ -n "${NBHD_API_BASE_URL:-}" ] && [ -n "${NBHD_INTERNAL_API_KEY:-}" ] && [ -n "${NBHD_TENANT_ID:-}" ]; then
        # Wait for the gateway's HTTP surface to come up (max ~60s).
        for _hook_attempt in $(seq 1 30); do
            if curl -sS -f -m 2 "http://127.0.0.1:18789/healthz" >/dev/null 2>&1; then
                break
            fi
            sleep 2
        done
        URL="${NBHD_API_BASE_URL%/}/api/cron/runtime/${NBHD_TENANT_ID}/container-started/"
        curl -sS -X POST -m 10 \
            -H "X-NBHD-Internal-Key: ${NBHD_INTERNAL_API_KEY}" \
            -H "X-NBHD-Tenant-Id: ${NBHD_TENANT_ID}" \
            -H "Content-Length: 0" \
            "$URL" \
            >/dev/null 2>&1 \
            && echo "[entrypoint] container-started hook OK" \
            || echo "[entrypoint] container-started hook failed (non-fatal)" >&2
    fi
) &

# --- BYO Anthropic pre-warm: keep the claude-cli session hot ---
#
# The first user turn after a cold start is brutal (~150s observed) because
# the `claude` subprocess hasn't been spawned yet and all 7 MCP plugins
# initialize sequentially the moment the gateway dispatches the first
# `/v1/chat/completions` request.
#
# Mitigation: as soon as the gateway is reachable, fire one benign
# /v1/chat/completions POST with a dedicated `user` param so it lands in
# its own isolated session (NOT the user's main thread — no history
# pollution). The model just replies "ok" or similar; what matters is the
# `claude` binary + plugin pool stay warm for the subsequent real turn.
#
# Cost: BYO routes through the tenant's own Anthropic Pro/Max
# subscription, so this counts toward their extra-usage credits — but a
# single noop turn is ~$0.001, well below the perceived-latency value.
#
# Only runs when CLAUDE_CODE_OAUTH_TOKEN is set (i.e. only BYO tenants
# pay the cost). Non-BYO tenants don't have this latency profile because
# they hit OpenRouter/MiniMax which is always-on remote inference, not a
# subprocess pool. Fully fire-and-forget; never blocks gateway startup.
(
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -n "${NBHD_INTERNAL_API_KEY:-}" ]; then
        # Stagger ~5s after the container-started hook so we don't race
        # the first real cron tick or hot-reload from Django.
        sleep 5
        for _warmup_attempt in $(seq 1 30); do
            if curl -sS -f -m 2 "http://127.0.0.1:18789/healthz" >/dev/null 2>&1; then
                break
            fi
            sleep 2
        done
        # Use a sentinel `user` value so workspace_routing on the Django
        # side never matches it AND the conversation lands in its own
        # isolated session/log file — invisible from the user's history.
        # Generous timeout (180s) because the first claude spawn IS the
        # slow path we're warming up.
        if curl -sS -m 180 \
            -H "Authorization: Bearer ${NBHD_INTERNAL_API_KEY}" \
            -H "Content-Type: application/json" \
            -H "X-Channel: warmup" \
            --data '{"model":"openclaw","user":"__nbhd_byo_warmup__","messages":[{"role":"user","content":"[warmup ping — reply with the single word OK and stop. Do not load any context, do not call any tools, do not write to memory or daily notes.]"}]}' \
            "http://127.0.0.1:18789/v1/chat/completions" \
            >/dev/null 2>&1; then
            echo "[entrypoint] BYO claude-cli pre-warm OK"
        else
            echo "[entrypoint] BYO claude-cli pre-warm failed (non-fatal)" >&2
        fi
    fi
) &

# Forward termination signals to both children
trap 'kill $GATEWAY_PID $PROXY_PID 2>/dev/null; wait' SIGTERM SIGINT

# Wait for either child to exit, then shut down the other
wait -n "$GATEWAY_PID" "$PROXY_PID" 2>/dev/null
EXIT_CODE=$?
echo "[entrypoint] child exited with code $EXIT_CODE, shutting down"
kill "$GATEWAY_PID" "$PROXY_PID" 2>/dev/null || true
wait
exit "$EXIT_CODE"
