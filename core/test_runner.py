"""
Test runner for the Lundrii regression suite.

This suite is meant to be run on *every* backend change, so it has to be fast
and it must not depend on whatever is in the developer's local `.env`. The
runner pins the environment-sensitive settings before any test loads:

* SQLite as the test database, even when `.env` sets DATABASE_URL to Neon.
  The pin happens before Django creates test databases, so the suite never
  opens a remote Postgres.
* A cheap password hasher. Django 6.1 runs PBKDF2 at 1,500,000 iterations,
  and the suite creates hundreds of users — that alone costs minutes. Password
  *behaviour* is unaffected: hashing, verification and `check_password()` are
  all still exercised, just with a cheaper KDF.
* LocMemCache, so a machine with CACHE_BACKEND=db in `.env` still runs the same
  tests. The DatabaseCache backend is covered explicitly in
  tests/test_infra_cache.py via override_settings.
* The immediate task backend, so enqueued Tasks run inline and can be asserted
  on. tests/test_infra_tasks.py overrides this where it needs the dummy backend.
* No Resend API key, so no test can reach the network.
"""

from django.conf import settings
from django.db import connections
from django.test.runner import DiscoverRunner


def _sqlite_test_database():
    return {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": settings.BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA foreign_keys=ON;"
            ),
        },
        "TEST": {"NAME": settings.BASE_DIR / ".test_db.sqlite3"},
    }


class LundriiTestRunner(DiscoverRunner):
    def _force_sqlite_database(self):
        """Ignore DATABASE_URL so tests never talk to Neon."""
        settings.DATABASES["default"] = _sqlite_test_database()
        connections.close_all()
        # Django 6 caches ConnectionHandler.settings as a cached_property; clearing
        # only `_settings` leaves the stale postgres dict (and Neon test DB name).
        connections.__dict__.pop("settings", None)
        connections._settings = None
        for alias in list(connections):
            try:
                del connections[alias]
            except AttributeError:
                pass

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)

        self._force_sqlite_database()
        settings.PASSWORD_HASHERS = [
            "django.contrib.auth.hashers.MD5PasswordHasher",
        ]
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "lundrii-test",
            }
        }
        settings.TASKS = {
            "default": {
                "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
            }
        }
        settings.RESEND_API_KEY = ""

    def setup_databases(self, **kwargs):
        self._force_sqlite_database()
        return super().setup_databases(**kwargs)
