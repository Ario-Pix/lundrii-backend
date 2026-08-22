#!/usr/bin/env bash
#
# Full backend regression gate. Run this after every backend change.
#
#   ./run_tests.sh                  # everything
#   ./run_tests.sh tests.test_auth  # one module, while iterating
#
# Three gates, in order — each is a way the backend can break without any test
# failing on its own:
#   1. system checks    — settings, apps and URLs still load
#   2. migration check  — models and migrations are still in sync
#   3. tests/           — every API and behaviour still does what it did
#
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="./.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
    echo "note: ./.venv not found, falling back to $PYTHON"
fi

echo "==> Django system checks"
"$PYTHON" manage.py check

echo "==> Migration state"
"$PYTHON" manage.py makemigrations --check --dry-run

echo "==> Test suite"
if [ "$#" -gt 0 ]; then
    "$PYTHON" manage.py test "$@"
else
    "$PYTHON" manage.py test
fi

echo
echo "All backend checks passed."
