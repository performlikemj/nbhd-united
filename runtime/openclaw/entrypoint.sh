#!/usr/bin/env sh
set -eu

OPENCLAW_HOME="${OPENCLAW_HOME:-/home/node/.openclaw}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-$OPENCLAW_HOME/openclaw.json}"
OPENCLAW_WORKSPACE_PATH="${OPENCLAW_WORKSPACE_PATH:-$OPENCLAW_HOME/workspace}"

mkdir -p "$OPENCLAW_HOME" "$OPENCLAW_WORKSPACE_PATH"

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
