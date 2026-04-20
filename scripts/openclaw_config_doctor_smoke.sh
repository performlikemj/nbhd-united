#!/usr/bin/env bash
# OpenClaw config doctor smoke test.
#
# Boots a disposable pgvector/pgvector:pg16 container (matching CI — see the
# openclaw-config-smoke job in .github/workflows/ci-cd.yml), migrates the schema,
# generates a tenant config, runs the Python validator, and then exercises the
# upstream `openclaw doctor` CLI against the generated JSON.
#
# Set DATABASE_URL to an existing Postgres URL to skip the Docker step.

set -euo pipefail

TMP_DIR="$(mktemp -d)"
CONFIG_PATH="$TMP_DIR/openclaw.json"
STATE_DIR="$TMP_DIR/state"
PYTHON_BIN="${PYTHON_BIN:-python}"

if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
fi

PG_CONTAINER=""
cleanup() {
  rm -rf "$TMP_DIR"
  if [ -n "$PG_CONTAINER" ]; then
    docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [ -z "${DATABASE_URL:-}" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker not found — install Docker or set DATABASE_URL to a running Postgres" >&2
    exit 1
  fi

  PG_CONTAINER="openclaw-smoke-$$"
  PG_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"

  echo "Starting pgvector/pgvector:pg16 on 127.0.0.1:$PG_PORT ($PG_CONTAINER)..."
  docker run -d --rm \
    --name "$PG_CONTAINER" \
    -e POSTGRES_DB=smoke_db \
    -e POSTGRES_USER=smoke_user \
    -e POSTGRES_PASSWORD=smoke_password \
    -p "127.0.0.1:$PG_PORT:5432" \
    pgvector/pgvector:pg16 >/dev/null

  echo -n "Waiting for Postgres"
  for i in $(seq 1 60); do
    if docker exec "$PG_CONTAINER" pg_isready -U smoke_user -d smoke_db >/dev/null 2>&1; then
      echo " — ready"
      break
    fi
    sleep 1
    echo -n "."
    if [ "$i" = "60" ]; then
      echo " — timed out waiting for Postgres" >&2
      exit 1
    fi
  done

  export DATABASE_URL="postgres://smoke_user:smoke_password@127.0.0.1:$PG_PORT/smoke_db"
fi

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

# `openclaw doctor` requires Node 22.12+. If the current node is too old, try
# to source nvm and switch to a Node 22 install.
node_major() { node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'; }
if [ "$(node_major)" -lt 22 ] 2>/dev/null; then
  NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [ -s "$NVM_DIR/nvm.sh" ]; then
    # shellcheck disable=SC1090,SC1091
    . "$NVM_DIR/nvm.sh"
    nvm use 22 >/dev/null 2>&1 || nvm use --lts >/dev/null 2>&1 || true
  fi
  if [ "$(node_major)" -lt 22 ] 2>/dev/null; then
    echo "openclaw doctor requires Node 22.12+ (current: $(node --version))." >&2
    echo "Install Node 22 (e.g. 'nvm install 22') or run this script in a shell with Node 22 on PATH." >&2
    exit 1
  fi
fi

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
