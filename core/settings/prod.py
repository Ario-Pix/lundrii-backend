import os

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

# Production uses SQLite (backend/db.sqlite3) baked into the Docker image.
# Do not set DATABASE_URL on Railway — leave unset so base.py keeps SQLite.

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
SECURE_SSL_REDIRECT = not os.getenv("RAILWAY_ENVIRONMENT")
SIMPLE_JWT["SIGNING_KEY"] = SECRET_KEY
