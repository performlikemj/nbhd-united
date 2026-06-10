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
# Worker config (issue #693 OOM mitigation):
# - gthread (not sync) so the PII model (~600 MB resident) is loaded once
#   per process and shared across threads, capping per-container PII memory
#   at 2 × 600 MB = 1.2 GB instead of 4 × 600 MB = 2.4 GB > cgroup limit.
# - 2 workers × 8 threads = 16 concurrent requests. Threads are cheap under
#   gthread (the PII model is per-PROCESS; thread count doesn't change the
#   1.2 GB memory math). Raised from 4 threads because chat drains hold a
#   slot for their full 120-240s tenant-container proxy call — a few
#   concurrent turns plus a cron sweep could starve dashboard traffic.
# - max-requests recycles each worker after ~1000 requests (±100 jitter so
#   they don't all recycle simultaneously) — bounds the long-tail memory
#   growth that drove the May 24 SIGKILL incident.
gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 \
  --worker-class gthread \
  --workers 2 \
  --threads 8 \
  --timeout 300 \
  --max-requests 1000 \
  --max-requests-jitter 100 \
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
