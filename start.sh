#!/bin/sh
set -eu

if [ -z "${DATABASE_URL:-}" ]; then
  echo "ERROR: DATABASE_URL is required." >&2
  echo "Set a postgres:// or postgresql:// URL (Neon) on Railway." >&2
  exit 1
fi

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-core.settings.prod}"

python manage.py migrate --noinput

echo "Starting gunicorn on port ${PORT:-8000}…"
exec gunicorn core.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
