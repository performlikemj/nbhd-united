#!/bin/bash
set -e

echo "Running database migrations..."
DATABASE_URL="${ADMIN_DATABASE_URL:-$DATABASE_URL}" python manage.py migrate --noinput

echo "Disabling RLS on any new tables..."
DATABASE_URL="${ADMIN_DATABASE_URL:-$DATABASE_URL}" python manage.py disable_rls || true

echo "Bumping pending config versions..."
python manage.py bump_pending_configs

echo "Starting central Telegram poller..."
python manage.py poll_telegram &
POLLER_PID=$!

echo "Starting gunicorn..."
gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers 2 \
  --timeout 120 \
  --access-logfile - \
  --access-logformat '%(h)s %(m)s %(U)s %(s)s %(D)sµs' &
GUNICORN_PID=$!

# Forward signals to both processes
trap 'kill $POLLER_PID $GUNICORN_PID 2>/dev/null; wait' SIGTERM SIGINT

# If the poller dies, restart it; only exit if gunicorn dies
while true; do
  wait -n "$POLLER_PID" "$GUNICORN_PID" 2>/dev/null
  EXIT_CODE=$?

  # Check if gunicorn died — that's fatal
  if ! kill -0 "$GUNICORN_PID" 2>/dev/null; then
    echo "[startup] gunicorn exited ($EXIT_CODE), shutting down"
    kill "$POLLER_PID" 2>/dev/null || true
    wait
    exit "$EXIT_CODE"
  fi

  # Poller died — restart it after a brief delay
  echo "[startup] poller exited ($EXIT_CODE), restarting in 5s..."
  sleep 5
  python manage.py poll_telegram &
  POLLER_PID=$!
done
