#!/usr/bin/env bash
# Local DB-backed iteration; use docker-gate for pre-push CI parity.
set -euo pipefail

if [ -x ./.venv/bin/python ]; then
  PY=./.venv/bin/python
else
  common_dir="$(git rev-parse --git-common-dir)"
  if [ -x "$common_dir/../.venv/bin/python" ]; then
    PY="$common_dir/../.venv/bin/python"
  else
    PY=python3
  fi
fi

test_env_file="${NBHD_TEST_ENV:-$HOME/.config/nbhd-united/test.env}"
if [[ -f "$test_env_file" ]]; then
  # Bash quotes export -p for replay, preserving even empty/multiline values.
  # Restore the caller's exports after loading machine-local defaults.
  caller_exports="$(export -p)"
  set -a
  # shellcheck disable=SC1090
  source "$test_env_file"
  set +a
  eval "$caller_exports"
  unset caller_exports
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  printf 'DATABASE_URL is required. Export it or add it to %s, for example:\n' "$test_env_file" >&2
  printf 'DATABASE_URL=postgres://your_role@127.0.0.1:5432/postgres\n' >&2
  exit 1
fi

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE-config.settings.development}"
export SECRET_KEY="${SECRET_KEY-test-secret-key-not-for-production}"
export DEBUG="${DEBUG-True}"
export AZURE_MOCK="${AZURE_MOCK-true}"
export NBHD_DISABLE_BACKGROUND_THREADS="${NBHD_DISABLE_BACKGROUND_THREADS-True}"

if [[ -z "${DJANGO_TEST_DB_NAME+x}" ]]; then
  worktree_name="$(basename "$(git rev-parse --show-toplevel)")"
  worktree_name="$(printf '%s' "$worktree_name" | LC_ALL=C tr '[:upper:]' '[:lower:]' | LC_ALL=C tr -c 'a-z0-9_' '_')"
  export DJANGO_TEST_DB_NAME="test_nbhd_${worktree_name:0:53}"
fi
if [[ -z "$DJANGO_TEST_DB_NAME" || "$DJANGO_TEST_DB_NAME" == test_nbhd_united_train ]]; then
  printf 'DJANGO_TEST_DB_NAME must be non-empty and must not be test_nbhd_united_train.\n' >&2
  exit 1
fi

"$PY" - <<'PY'
import os
import sys
from urllib.parse import urlsplit

try:
    database = urlsplit(os.environ["DATABASE_URL"])
    host = database.hostname or "(local socket)"
except ValueError:
    sys.exit("DATABASE_URL could not be parsed; check its format.")
print(f"Test DB: {os.environ['DJANGO_TEST_DB_NAME']}", flush=True)
print(f"DATABASE_URL host: {host}; database: {database.path.lstrip('/')}", flush=True)
PY

exec "$PY" manage.py test "${@:-apps/}" --noinput
