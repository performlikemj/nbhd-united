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

# Wait for either to exit, then shut down the other
wait -n "$POLLER_PID" "$GUNICORN_PID" 2>/dev/null
EXIT_CODE=$?
echo "[startup] child exited with code $EXIT_CODE, shutting down"
kill "$POLLER_PID" "$GUNICORN_PID" 2>/dev/null || true
wait
exit "$EXIT_CODE"
