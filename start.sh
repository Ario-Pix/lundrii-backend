#!/bin/sh
set -eu

DB_SRC="/app/db.sqlite3"
DB_DIR="/tmp/lundrii-data"
DB_DEST="$DB_DIR/db.sqlite3"

if [ ! -f "$DB_SRC" ]; then
  echo "ERROR: $DB_SRC not found in container image." >&2
  echo "Commit backend/db.sqlite3 and redeploy." >&2
  exit 1
fi

mkdir -p "$DB_DIR"
cp "$DB_SRC" "$DB_DEST"
export LUNDRII_DB_PATH="$DB_DEST"

echo "Starting gunicorn on port ${PORT:-8000} (SQLite: $DB_DEST)…"
exec gunicorn core.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
