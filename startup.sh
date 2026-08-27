#!/bin/bash
set -e

PII_TRANSPORT="${PII_DETECTOR_TRANSPORT:-local}"
PII_SOCKET="${PII_SHARED_SOCKET:-/run/nbhd/pii-detector.sock}"
SIDECAR_PID=""
POLLER_PID=""
GUNICORN_PID=""
SIDECAR_BACKOFF_S="${NBHD_STARTUP_BACKOFF_BASE_S:-5}"
SIDECAR_BACKOFF_CAP_S="${NBHD_STARTUP_BACKOFF_CAP_S:-60}"

echo "Running database migrations..."
DATABASE_URL="${ADMIN_DATABASE_URL:-$DATABASE_URL}" python manage.py migrate --noinput
echo "Disabling RLS on any new tables..."
DATABASE_URL="${ADMIN_DATABASE_URL:-$DATABASE_URL}" python manage.py disable_rls || true
echo "Bumping pending config versions..."
python manage.py bump_pending_configs

start_sidecar() {
  if [ "$PII_TRANSPORT" != "shared" ]; then
    SIDECAR_PID=""
    return
  fi
  if [ -S "$PII_SOCKET" ]; then
    rm -f -- "$PII_SOCKET"
  fi
  echo "[startup] starting shared PII detector..."
  python -m apps.pii.shared_server &
  SIDECAR_PID=$!
}

start_poller() {
  echo "Starting central Telegram poller..."
  python manage.py poll_telegram &
  POLLER_PID=$!
}

start_gunicorn() {
  echo "Starting gunicorn..."
  gunicorn config.wsgi:application \
    -c gunicorn.conf.py \
    --bind 0.0.0.0:8000 \
    --worker-class gthread \
    --workers 2 \
    --threads 8 \
    --timeout 300 \
    --graceful-timeout 600 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --access-logfile - \
    --access-logformat '%(h)s %(m)s %(U)s %(s)s %(D)sµs' &
  GUNICORN_PID=$!
}

shutdown_children() {
  code="$1"
  trap - SIGTERM SIGINT
  for pid in "$SIDECAR_PID" "$POLLER_PID" "$GUNICORN_PID"; do
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$SIDECAR_PID" "$POLLER_PID" "$GUNICORN_PID"; do
    if [ -n "$pid" ]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
  exit "$code"
}

trap 'shutdown_children 143' SIGTERM
trap 'shutdown_children 130' SIGINT

if /bin/bash -c 'help wait' 2>/dev/null | grep -q -- '-n'; then
  WAIT_N_SUPPORTED=1
  echo "[startup] supervisor wait mode=wait-n"
else
  WAIT_N_SUPPORTED=0
  echo "[startup] supervisor wait mode=poll"
fi

wait_for_any_child() {
  if [ "$WAIT_N_SUPPORTED" = "1" ]; then
    rc=0
    wait -n "$@" 2>/dev/null || rc=$?
    WAIT_RC=$rc
    return
  fi
  while true; do
    for pid in "$@"; do
      if ! kill -0 "$pid" 2>/dev/null; then
        rc=0
        wait "$pid" 2>/dev/null || rc=$?
        WAIT_RC=$rc
        return
      fi
    done
    sleep 0.1
  done
}

start_sidecar
start_poller
start_gunicorn

while true; do
  if [ -n "$SIDECAR_PID" ]; then
    wait_for_any_child "$SIDECAR_PID" "$POLLER_PID" "$GUNICORN_PID"
  else
    wait_for_any_child "$POLLER_PID" "$GUNICORN_PID"
  fi

  if ! kill -0 "$GUNICORN_PID" 2>/dev/null; then
    echo "[startup] gunicorn exited ($WAIT_RC), shutting down"
    shutdown_children "$WAIT_RC"
  fi
  if [ -n "$SIDECAR_PID" ] && ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
    echo "[startup] PII detector exited ($WAIT_RC), restarting in ${SIDECAR_BACKOFF_S}s..."
    sleep "$SIDECAR_BACKOFF_S"
    start_sidecar
    SIDECAR_BACKOFF_S=$((SIDECAR_BACKOFF_S * 2))
    if [ "$SIDECAR_BACKOFF_S" -gt "$SIDECAR_BACKOFF_CAP_S" ]; then
      SIDECAR_BACKOFF_S="$SIDECAR_BACKOFF_CAP_S"
    fi
    continue
  fi
  if ! kill -0 "$POLLER_PID" 2>/dev/null; then
    echo "[startup] poller exited ($WAIT_RC), restarting in 5s..."
    sleep 5
    start_poller
  fi
done
