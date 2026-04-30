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

if [ -d "$NBHD_MANAGED_SKILLS_SRC" ]; then
    rm -rf "$NBHD_MANAGED_SKILLS_DST"
    mkdir -p "$NBHD_MANAGED_SKILLS_DST"
    cp -R "$NBHD_MANAGED_SKILLS_SRC"/. "$NBHD_MANAGED_SKILLS_DST"/
fi

# AGENTS.md — always overwritten (system-controlled)
# Prefer persona-rendered content from env var; fall back to static template
if [ -n "${NBHD_AGENTS_MD:-}" ]; then
    printf '%s\n' "$NBHD_AGENTS_MD" > "$NBHD_MANAGED_AGENTS_DST"
elif [ -f "$NBHD_MANAGED_AGENTS_TEMPLATE" ]; then
    cp "$NBHD_MANAGED_AGENTS_TEMPLATE" "$NBHD_MANAGED_AGENTS_DST"
fi

# Skill templates.md — overwrite with tenant-specific content from env var
if [ -n "${NBHD_SKILL_TEMPLATES_MD:-}" ]; then
    SKILL_TEMPLATES_DST="${NBHD_MANAGED_SKILLS_DST}/daily-journal/references/templates.md"
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
            if curl -sS -f -m 2 "http://127.0.0.1:18789/health" >/dev/null 2>&1; then
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

# Forward termination signals to both children
trap 'kill $GATEWAY_PID $PROXY_PID 2>/dev/null; wait' SIGTERM SIGINT

# Wait for either child to exit, then shut down the other
wait -n "$GATEWAY_PID" "$PROXY_PID" 2>/dev/null
EXIT_CODE=$?
echo "[entrypoint] child exited with code $EXIT_CODE, shutting down"
kill "$GATEWAY_PID" "$PROXY_PID" 2>/dev/null || true
wait
exit "$EXIT_CODE"
