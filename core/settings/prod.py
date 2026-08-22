import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = os.getenv("SECRET_KEY", "")

_PLACEHOLDER_SECRET_KEYS = {
    "",
    "django-insecure-dev-only-change-me",
    "change-me-to-a-long-random-string",
}
if SECRET_KEY.strip() in _PLACEHOLDER_SECRET_KEYS:
    raise ImproperlyConfigured(
        "SECRET_KEY must be a non-placeholder value when DEBUG is False."
    )

# Production always uses SQLite — ignore DATABASE_URL even if Railway injects Postgres.
_db_path = Path(os.getenv("LUNDRII_DB_PATH", str(BASE_DIR / "db.sqlite3")))
if not _db_path.is_file():
    raise ImproperlyConfigured(
        f"db.sqlite3 not found at {_db_path}. Commit backend/db.sqlite3 and ensure "
        "it is not excluded by .dockerignore."
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _db_path,
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA foreign_keys=ON;"
            ),
        },
    }
}

ALLOWED_HOSTS = [
    "lundrii-backend-production.up.railway.app",
    "healthcheck.railway.app",
    ".up.railway.app",
]
CORS_ALLOWED_ORIGINS = [
    "https://lundrii-web-application.vercel.app",
]
FRONTEND_URL = "https://lundrii-web-application.vercel.app"

CACHE_BACKEND = os.getenv("CACHE_BACKEND", "db")
CACHES = build_caches(CACHE_BACKEND)
CSRF_TRUSTED_ORIGINS = build_csrf_trusted_origins(
    cors_origins=CORS_ALLOWED_ORIGINS,
    frontend_url=FRONTEND_URL,
    allowed_hosts=ALLOWED_HOSTS,
    use_https=True,
    extra_origins=[f"https://{ALLOWED_HOSTS[0]}"],
)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# Railway terminates TLS at the edge; internal health probes use HTTP.
SECURE_SSL_REDIRECT = False
SIMPLE_JWT["SIGNING_KEY"] = SECRET_KEY
