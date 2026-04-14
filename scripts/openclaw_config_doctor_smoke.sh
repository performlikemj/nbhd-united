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

export DATABASE_URL="${DATABASE_URL:-sqlite:///$DB_PATH}"
export AZURE_MOCK="true"

# Disable plugins whose paths only exist in the OpenClaw container image,
# not in CI.  The default for OPENCLAW_USAGE_PLUGIN_ID is non-empty
# ("nbhd-usage-reporter"), so we must explicitly clear it here.
export OPENCLAW_USAGE_PLUGIN_ID=""

$PYTHON_BIN manage.py migrate --noinput >/dev/null

# Generate config and run Python validator
$PYTHON_BIN manage.py shell -c "
import json
import pathlib
from apps.tenants.services import create_tenant
from apps.orchestrator.config_generator import generate_openclaw_config
from apps.orchestrator.config_validator import validate_openclaw_config

tenant = create_tenant(display_name='OpenClaw Doctor Smoke', telegram_chat_id=999001)
config = generate_openclaw_config(tenant)

# Run Python validator — catches semantic issues (plugin wiring, gateway security, etc.)
issues = validate_openclaw_config(config)
errors = [i for i in issues if i.severity == 'error']
if errors:
    print(f'FAIL: config has {len(errors)} validation error(s):')
    for e in errors:
        print(f'  {e.path}: {e.message}')
    raise SystemExit(1)
warnings = [i for i in issues if i.severity == 'warning']
print(f'PASS: config validator ({len(warnings)} warnings)')

pathlib.Path('$CONFIG_PATH').write_text(json.dumps(config))
"

chmod 600 "$CONFIG_PATH"
mkdir -p "$STATE_DIR"

set +e
DOCTOR_OUTPUT="$(
  OPENCLAW_CONFIG_PATH="$CONFIG_PATH" \
  OPENCLAW_STATE_DIR="$STATE_DIR" \
  OPENCLAW_VERSION=$(grep -oP 'ARG OPENCLAW_VERSION=\K.*' Dockerfile.openclaw 2>/dev/null || echo "latest")
  npx --yes --package "openclaw@${OPENCLAW_VERSION}" openclaw doctor --non-interactive 2>&1
)"
DOCTOR_EXIT=$?
set -e

printf '%s\n' "$DOCTOR_OUTPUT"

if printf '%s\n' "$DOCTOR_OUTPUT" | grep -qi "Invalid config"; then
  echo "OpenClaw config doctor smoke failed: invalid config detected." >&2
  exit 1
fi

if [ "$DOCTOR_EXIT" -ne 0 ]; then
  echo "OpenClaw config doctor smoke failed: doctor command exited non-zero." >&2
  exit "$DOCTOR_EXIT"
fi
