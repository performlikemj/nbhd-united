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
  caller_exports="$(
    # Readonly values cannot be overwritten; exclude them from replay.
    # Clear only the subshell's export attributes, keeping Bash's safe quoting.
    while IFS= read -r export_name; do
      if [[ "$(declare -p "$export_name")" =~ ^declare\ -[^[:space:]]*r ]]; then
        export -n "$export_name"
      fi
    done < <(compgen -e)
    export -p
  )"
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

check_override=1
if [[ -z "${DJANGO_TEST_DB_NAME+x}" ]]; then
  check_override=0
  worktree_path="$(git rev-parse --show-toplevel)"
  worktree_hash="$("$PY" -c 'import hashlib, os, sys; print(hashlib.sha256(os.fsencode(sys.argv[1])).hexdigest()[:6])' "$worktree_path")"
  worktree_name="$(basename "$worktree_path")"
  worktree_name="$(printf '%s' "$worktree_name" | LC_ALL=C tr '[:upper:]' '[:lower:]' | LC_ALL=C tr -c 'a-z0-9_' '_')"
  # 10-character prefix + 41-character basename + separator + 6 hex digits.
  export DJANGO_TEST_DB_NAME="test_nbhd_${worktree_name:0:41}_${worktree_hash}"
fi
if [[ ! "$DJANGO_TEST_DB_NAME" =~ ^test_nbhd_[a-z0-9_]+$ || ${#DJANGO_TEST_DB_NAME} -gt 58 || "$DJANGO_TEST_DB_NAME" == test_nbhd_united_train ]]; then
  printf 'DJANGO_TEST_DB_NAME must match ^test_nbhd_[a-z0-9_]+$, be at most 58 characters (leaving room for Django parallel clone suffixes _NNNN), and must not be test_nbhd_united_train: Django --noinput can drop an existing database on this shared Postgres server.\n' >&2
  exit 1
fi

"$PY" - "$check_override" <<'PY'
import os
import sys
from urllib.parse import urlsplit

if sys.argv[1] == "1":
    try:
        import psycopg

        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
            exists = connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (os.environ["DJANGO_TEST_DB_NAME"],),
            ).fetchone() is not None
    except Exception:
        # Connection errors can contain credentials; never echo their details.
        sys.exit("Refusing DJANGO_TEST_DB_NAME override: safety could not be verified (psycopg import, connection, or existence query failed).")
    if exists and os.environ.get("NBHD_TEST_DB_REUSE") != "1":
        sys.exit("Refusing DJANGO_TEST_DB_NAME override: database already exists. Django --noinput can drop it; set NBHD_TEST_DB_REUSE=1 only to explicitly allow reuse.")

try:
    database = urlsplit(os.environ["DATABASE_URL"])
    host = database.hostname or "(local socket)"
except ValueError:
    sys.exit("DATABASE_URL could not be parsed; check its format.")
print(f"Test DB: {os.environ['DJANGO_TEST_DB_NAME']}", flush=True)
print(f"DATABASE_URL host: {host}; database: {database.path.lstrip('/')}", flush=True)
PY

exec "$PY" manage.py test "${@:-apps/}" --noinput
