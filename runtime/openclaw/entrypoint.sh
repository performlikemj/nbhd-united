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

if [ -n "${OPENCLAW_CONFIG_JSON:-}" ]; then
    printf '%s\n' "$OPENCLAW_CONFIG_JSON" > "$OPENCLAW_CONFIG_PATH"
fi

# Inject webhook secret into config (never stored in config JSON at rest)
if [ -n "${OPENCLAW_WEBHOOK_SECRET:-}" ]; then
    node -e "
      const fs = require('fs');
      const p = '$OPENCLAW_CONFIG_PATH';
      const c = JSON.parse(fs.readFileSync(p, 'utf8'));
      if (c.channels && c.channels.telegram) {
        c.channels.telegram.webhookSecret = process.env.OPENCLAW_WEBHOOK_SECRET;
      }
      fs.writeFileSync(p, JSON.stringify(c, null, 2));
    "
fi

if [ ! -f "$OPENCLAW_CONFIG_PATH" ]; then
    echo "OPENCLAW_CONFIG_JSON is not set and config file is missing at $OPENCLAW_CONFIG_PATH" >&2
    exit 1
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

# Start both processes in background
# shellcheck disable=SC2086
openclaw gateway --allow-unconfigured $GATEWAY_ARGS &
GATEWAY_PID=$!

node /opt/nbhd/proxy.js &
PROXY_PID=$!

# Forward termination signals to both children
trap 'kill $GATEWAY_PID $PROXY_PID 2>/dev/null; wait' SIGTERM SIGINT

# Wait for either child to exit, then shut down the other
wait -n "$GATEWAY_PID" "$PROXY_PID" 2>/dev/null
EXIT_CODE=$?
echo "[entrypoint] child exited with code $EXIT_CODE, shutting down"
kill "$GATEWAY_PID" "$PROXY_PID" 2>/dev/null || true
wait
exit "$EXIT_CODE"
