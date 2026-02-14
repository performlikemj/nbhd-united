#!/usr/bin/env bash
set -euo pipefail

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

DB_PATH="$TMP_DIR/test.sqlite3"
CONFIG_PATH="$TMP_DIR/openclaw.json"
STATE_DIR="$TMP_DIR/state"
PYTHON_BIN="${PYTHON_BIN:-python}"

if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
fi

export DATABASE_URL="sqlite:///$DB_PATH"
export AZURE_MOCK="true"

$PYTHON_BIN manage.py migrate --noinput >/dev/null
$PYTHON_BIN manage.py shell -c "
import json
import pathlib
from apps.tenants.services import create_tenant
from apps.orchestrator.config_generator import generate_openclaw_config

tenant = create_tenant(display_name='OpenClaw Doctor Smoke', telegram_chat_id=999001)
pathlib.Path('$CONFIG_PATH').write_text(json.dumps(generate_openclaw_config(tenant)))
"

chmod 600 "$CONFIG_PATH"
mkdir -p "$STATE_DIR"

DOCTOR_OUTPUT="$(
  OPENCLAW_CONFIG_PATH="$CONFIG_PATH" \
  OPENCLAW_STATE_DIR="$STATE_DIR" \
  npx --yes openclaw doctor --non-interactive --no-workspace-suggestions 2>&1
)"

printf '%s\n' "$DOCTOR_OUTPUT"

if printf '%s\n' "$DOCTOR_OUTPUT" | grep -qi "Invalid config"; then
  echo "OpenClaw config doctor smoke failed: invalid config detected." >&2
  exit 1
fi
