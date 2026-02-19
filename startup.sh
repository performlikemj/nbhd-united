#!/bin/bash
set -e

echo "Running database migrations..."
DATABASE_URL="${ADMIN_DATABASE_URL:-$DATABASE_URL}" python manage.py migrate --noinput

echo "Starting gunicorn..."
exec gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers 2 \
  --timeout 120 \
  --access-logfile - \
  --access-logformat '%(h)s %(m)s %(U)s %(s)s %(D)sµs'
