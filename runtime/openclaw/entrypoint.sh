#!/usr/bin/env sh
set -eu

OPENCLAW_HOME="${OPENCLAW_HOME:-/home/node/.openclaw}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-$OPENCLAW_HOME/openclaw.json}"
OPENCLAW_WORKSPACE_PATH="${OPENCLAW_WORKSPACE_PATH:-$OPENCLAW_HOME/workspace}"
NBHD_MANAGED_SKILLS_SRC="${NBHD_MANAGED_SKILLS_SRC:-/opt/nbhd/agent-skills}"
NBHD_MANAGED_SKILLS_DST="${NBHD_MANAGED_SKILLS_DST:-$OPENCLAW_WORKSPACE_PATH/skills/nbhd-managed}"
NBHD_MANAGED_AGENTS_TEMPLATE="${NBHD_MANAGED_AGENTS_TEMPLATE:-/opt/nbhd/templates/openclaw/AGENTS.md}"
NBHD_MANAGED_AGENTS_DST="${NBHD_MANAGED_AGENTS_DST:-$OPENCLAW_WORKSPACE_PATH/AGENTS.md}"

mkdir -p "$OPENCLAW_HOME" "$OPENCLAW_WORKSPACE_PATH"

if [ -d "$NBHD_MANAGED_SKILLS_SRC" ]; then
    rm -rf "$NBHD_MANAGED_SKILLS_DST"
    mkdir -p "$NBHD_MANAGED_SKILLS_DST"
    cp -R "$NBHD_MANAGED_SKILLS_SRC"/. "$NBHD_MANAGED_SKILLS_DST"/
fi

if [ -f "$NBHD_MANAGED_AGENTS_TEMPLATE" ]; then
    cp "$NBHD_MANAGED_AGENTS_TEMPLATE" "$NBHD_MANAGED_AGENTS_DST"
fi

if [ -n "${OPENCLAW_CONFIG_JSON:-}" ]; then
    printf '%s\n' "$OPENCLAW_CONFIG_JSON" > "$OPENCLAW_CONFIG_PATH"
fi

if [ ! -f "$OPENCLAW_CONFIG_PATH" ]; then
    echo "OPENCLAW_CONFIG_JSON is not set and config file is missing at $OPENCLAW_CONFIG_PATH" >&2
    exit 1
fi

if [ "$#" -gt 0 ]; then
    case "$1" in
        gateway)
            shift
            exec openclaw gateway --allow-unconfigured "$@"
            ;;
        -*)
            exec openclaw gateway --allow-unconfigured "$@"
            ;;
        *)
            exec openclaw "$@"
            ;;
    esac
fi

exec openclaw gateway --allow-unconfigured
