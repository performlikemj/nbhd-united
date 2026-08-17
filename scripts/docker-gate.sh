#!/usr/bin/env bash
# Run the workflow's Linux frontend-test and backend-test legs locally.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DOCKER_GATE_CACHE_DIR="${DOCKER_GATE_CACHE:-$HOME/.cache/nbhd-docker-gate}"
SNAPSHOT=""
NETWORK=""
POSTGRES_CONTAINER=""
BACKEND_CONTAINER=""
FRONTEND_CONTAINER=""

cleanup_backend() {
  if [[ -n "$BACKEND_CONTAINER" ]]; then
    docker rm -f "$BACKEND_CONTAINER" >/dev/null 2>&1 || true
    BACKEND_CONTAINER=""
  fi
  if [[ -n "$POSTGRES_CONTAINER" ]]; then
    docker rm -f "$POSTGRES_CONTAINER" >/dev/null 2>&1 || true
    POSTGRES_CONTAINER=""
  fi
  if [[ -n "$NETWORK" ]]; then
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
    NETWORK=""
  fi
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  cleanup_backend
  if [[ -n "$FRONTEND_CONTAINER" ]]; then
    docker rm -f "$FRONTEND_CONTAINER" >/dev/null 2>&1 || true
  fi
  if [[ -n "$SNAPSHOT" && -d "$SNAPSHOT" ]]; then
    rm -rf "$SNAPSHOT"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

create_snapshot() {
  SNAPSHOT="$(mktemp -d "${TMPDIR:-/tmp}/nbhd-docker-gate.XXXXXX")" || return 1
  (
    cd "$ROOT" || exit 1
    tar -cf - \
      --exclude='./.git' \
      --exclude='./.env' \
      --exclude='./.env.*' \
      --exclude='*/.env' \
      --exclude='*/.env.*' \
      --exclude='./.venv' \
      --exclude='./venv' \
      --exclude='./env' \
      --exclude='./frontend/node_modules' \
      --exclude='./frontend/.next' \
      --exclude='./frontend/out' \
      --exclude='./.pytest_cache' \
      --exclude='./.ruff_cache' \
      .
  ) | tar -xf - -C "$SNAPSHOT"
}

wait_for_postgres() {
  local state
  local attempt
  for attempt in {1..30}; do
    state="$(docker inspect --format '{{.State.Health.Status}}' "$POSTGRES_CONTAINER" 2>/dev/null)" || return 1
    case "$state" in
      healthy)
        return 0
        ;;
      unhealthy)
        docker logs "$POSTGRES_CONTAINER" >&2
        return 1
        ;;
    esac
    sleep 2
  done
  echo "Postgres did not become healthy within 60 seconds." >&2
  docker logs "$POSTGRES_CONTAINER" >&2
  return 1
}

run_backend() {
  NETWORK="nbhd-docker-gate-$$"
  POSTGRES_CONTAINER="nbhd-docker-gate-postgres-$$"
  BACKEND_CONTAINER="nbhd-docker-gate-backend-$$"

  docker network create "$NETWORK" >/dev/null || return 1
  docker run --detach --rm \
    --name "$POSTGRES_CONTAINER" \
    --network "$NETWORK" \
    --env POSTGRES_DB=test_db \
    --env POSTGRES_USER=test_user \
    --env POSTGRES_PASSWORD=test_password \
    --health-cmd pg_isready \
    --health-interval 10s \
    --health-timeout 5s \
    --health-retries 5 \
    pgvector/pgvector:pg16 >/dev/null || return 1
  wait_for_postgres || return 1

  local backend_script
  read -r -d '' backend_script <<'BACKEND_SCRIPT' || true
cp -a /repo/. /workspace

echo "--- Install dependencies ---"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  tzdata \
  tzdata-legacy
rm -rf /var/lib/apt/lists/*
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install "ruff==0.15.21"
pip install --no-deps https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl || python -m spacy download en_core_web_sm

echo "--- Run ruff lint ---"
ruff check .

echo "--- Run ruff format check ---"
ruff format --check .

echo "--- Secret scan ---"
HITS=$(grep -rPn '(sk-ant-[A-Za-z0-9_-]{20,}|sk-or-v1-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|AAAAAAAAAAAAA|tvly-dev-[A-Za-z0-9_-]{20,})' \
  --include='*.py' --include='*.ts' --include='*.tsx' --include='*.js' \
  --include='*.json' --include='*.yaml' --include='*.yml' \
  --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=.venv \
  --exclude='*.lock' \
  --exclude='config_validator.py' \
  --exclude='config_security.py' \
  --exclude='test_config_security.py' \
  --exclude='harness-check' \
  --exclude='ci-cd.yml' \
  . 2>/dev/null || true)
if [ -n "$HITS" ]; then
  echo "Secret patterns detected in source files:"
  echo "$HITS"
  exit 1
fi
echo "No secret patterns found"

echo "--- Run Django checks and tests ---"
(
  export DJANGO_SETTINGS_MODULE=config.settings.development
  export SECRET_KEY=test-secret-key-not-for-production
  export DEBUG=True
  export DATABASE_URL=postgres://test_user:test_password@localhost:5432/test_db
  export ALLOWED_HOSTS='*'
  export NBHD_DISABLE_BACKGROUND_THREADS=True
  export AZURE_MOCK=true
  python manage.py makemigrations --check --dry-run
  python manage.py migrate --noinput
  python manage.py check
  python manage.py test apps/
)

echo "--- Config validator check ---"
(
  export DJANGO_SETTINGS_MODULE=config.settings.development
  export SECRET_KEY=test-secret-key-not-for-production
  export DEBUG=True
  export DATABASE_URL=postgres://test_user:test_password@localhost:5432/test_db
  export ALLOWED_HOSTS='*'
  export AZURE_MOCK=true
  export OPENCLAW_USAGE_PLUGIN_ID=''
  python manage.py shell -c "
from apps.tenants.services import create_tenant
from apps.orchestrator.config_generator import generate_openclaw_config
from apps.orchestrator.config_validator import validate_openclaw_config
from apps.orchestrator.config_security import audit_config_security

tenant = create_tenant(display_name='CI Validator', telegram_chat_id=999999)
config = generate_openclaw_config(tenant)

issues = validate_openclaw_config(config)
errors = [i for i in issues if i.severity == 'error']
if errors:
    for e in errors:
        print(f'ERROR: {e.path} — {e.message}')
    raise SystemExit(1)

findings = audit_config_security(config)
sec_errors = [f for f in findings if f.severity == 'error']
if sec_errors:
    for f in sec_errors:
        print(f'SECURITY: {f.check} — {f.message}')
    raise SystemExit(1)

print('Config validator: PASS')
print('Security audit: PASS')
"
)
BACKEND_SCRIPT

  docker run --rm \
    --name "$BACKEND_CONTAINER" \
    --network "container:$POSTGRES_CONTAINER" \
    --mount "type=bind,source=$SNAPSHOT,target=/repo,readonly" \
    --volume "$DOCKER_GATE_CACHE_DIR/pip:/root/.cache/pip" \
    --workdir /workspace \
    python:3.12 \
    bash -euo pipefail -c "$backend_script"
}

run_frontend() {
  FRONTEND_CONTAINER="nbhd-docker-gate-frontend-$$"

  local frontend_script
  read -r -d '' frontend_script <<'FRONTEND_SCRIPT' || true
cp -a /repo/. /workspace
cd frontend

echo "--- Install frontend dependencies ---"
npm ci

echo "--- Lint frontend ---"
npm run lint

echo "--- Build frontend (static export) ---"
npm run build
FRONTEND_SCRIPT

  docker run --rm \
    --name "$FRONTEND_CONTAINER" \
    --mount "type=bind,source=$SNAPSHOT,target=/repo,readonly" \
    --volume "$DOCKER_GATE_CACHE_DIR/npm:/root/.npm" \
    --workdir /workspace \
    node:22 \
    bash -euo pipefail -c "$frontend_script"
}

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI is not installed or not on PATH." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is unavailable." >&2
  exit 1
fi
if ! mkdir -p "$DOCKER_GATE_CACHE_DIR/pip" "$DOCKER_GATE_CACHE_DIR/npm"; then
  echo "Failed to create docker-gate cache directories." >&2
  exit 1
fi
if ! create_snapshot; then
  echo "Failed to create the sanitized working-tree snapshot." >&2
  exit 1
fi

status=0

echo "=== BACKEND LEG: python:3.12 + pgvector/pgvector:pg16 ==="
if run_backend; then
  echo "=== BACKEND LEG: PASS ==="
else
  echo "=== BACKEND LEG: FAIL ===" >&2
  status=1
fi
cleanup_backend

echo
echo "=== FRONTEND LEG: node:22 ==="
if run_frontend; then
  echo "=== FRONTEND LEG: PASS ==="
else
  echo "=== FRONTEND LEG: FAIL ===" >&2
  status=1
fi
FRONTEND_CONTAINER=""

if [[ "$status" -eq 0 ]]; then
  echo
  echo "=== DOCKER CI-PARITY GATE: PASS ==="
else
  echo
  echo "=== DOCKER CI-PARITY GATE: FAIL ===" >&2
fi
exit "$status"
