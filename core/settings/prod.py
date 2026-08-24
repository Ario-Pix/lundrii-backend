import os
import sys

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import _postgres_from_url

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

# Production is Neon (or any Postgres) via DATABASE_URL. Committed db.sqlite3 is
# not the runtime database. collectstatic during the Docker image build has no
# DATABASE_URL; an in-memory SQLite is enough because collectstatic never opens
# a connection.
_prod_database_url = os.getenv("DATABASE_URL", "").strip()
if _prod_database_url:
    DATABASES = {"default": _postgres_from_url(_prod_database_url)}
elif "collectstatic" in sys.argv:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
else:
    raise ImproperlyConfigured(
        "DATABASE_URL is required in production. Set a postgres:// or "
        "postgresql:// URL (Neon). collectstatic during image build is the "
        "only exception."
    )

ALLOWED_HOSTS = [
    "lundrii-backend-production.up.railway.app",
    "healthcheck.railway.app",
    ".up.railway.app",
]
CORS_ALLOWED_ORIGINS = [
    "https://lundrii-web-application.vercel.app",
    "https://lundrii-admin-portal.vercel.app",
]
FRONTEND_URL = "https://lundrii-web-application.vercel.app"
ADMIN_FRONTEND_URL = os.getenv(
    "ADMIN_FRONTEND_URL", "https://lundrii-admin-portal.vercel.app"
)

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
