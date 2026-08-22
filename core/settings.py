"""
Django settings for Lundrii backend (core).
"""

import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from corsheaders.defaults import default_headers
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from base.apidocs import API_DESCRIPTION

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-dev-only-change-me")

DEBUG = os.getenv("DEBUG", "True").lower() in ("1", "true", "yes")
# Railway production must not serve debug tracebacks (DisallowedHost pages are ~70KB).
if os.getenv("RAILWAY_ENVIRONMENT") == "production":
    DEBUG = False

_PLACEHOLDER_SECRET_KEYS = {
    "",
    "django-insecure-dev-only-change-me",
    "change-me-to-a-long-random-string",
}
if not DEBUG and SECRET_KEY.strip() in _PLACEHOLDER_SECRET_KEYS:
    raise ImproperlyConfigured(
        "SECRET_KEY must be a non-placeholder value when DEBUG is False."
    )

ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]
# Railway injects RAILWAY_PUBLIC_DOMAIN (e.g. lundrii-backend-production.up.railway.app).
_railway_public = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
if _railway_public and _railway_public not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_railway_public)
# Any *.up.railway.app service hostname when deployed on Railway.
if os.getenv("RAILWAY_ENVIRONMENT") and ".up.railway.app" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(".up.railway.app")
# Railway health checks hit /health/ with Host: healthcheck.railway.app.
if "healthcheck.railway.app" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("healthcheck.railway.app")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "drf_spectacular",
    # Local
    "base",
    "authentication",
    "laundry",
    "mcp_server",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"
ASGI_APPLICATION = "core.asgi.application"

# ---------------------------------------------------------------------------
# Database
#
# The SQLite OPTIONS below are what make concurrent booking safe, not tuning
# knobs. Booking claims a slot by reading "is this free?" and writing inside one
# transaction (see laundry/services/booking.py):
#
#   transaction_mode IMMEDIATE — take the write lock when the transaction opens
#     rather than at first write. Under the default DEFERRED mode two writers
#     both start read-only, both pass the availability check, and the second is
#     rejected outright when it tries to upgrade — surfacing as "database is
#     locked" rather than a clean "slot taken".
#   timeout — how long a blocked writer waits for the lock instead of failing
#     immediately. Without it, contention is an error rather than a short queue.
#   WAL — readers no longer block on the writer, so browsing availability stays
#     responsive while someone is booking.
#
# On PostgreSQL none of this is needed: the SELECT … FOR UPDATE row lock in
# booking.py does the same job natively.
#
# Production (Railway): set DATABASE_URL to a postgres/postgresql URL.
# Local and tests keep SQLite when DATABASE_URL is unset.
# ---------------------------------------------------------------------------
_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def _postgres_from_url(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ImproperlyConfigured(
            "DATABASE_URL must be a postgres:// or postgresql:// URL."
        )
    query = parse_qs(parsed.query)
    options = {}
    sslmode = (query.get("sslmode") or [None])[0]
    if sslmode:
        options["sslmode"] = sslmode
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": 60,
        "OPTIONS": options,
    }


if _DATABASE_URL:
    DATABASES = {"default": _postgres_from_url(_DATABASE_URL)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {
                "transaction_mode": "IMMEDIATE",
                "timeout": 20,
                "init_command": (
                    "PRAGMA journal_mode=WAL;"
                    "PRAGMA synchronous=NORMAL;"
                    "PRAGMA foreign_keys=ON;"
                ),
            },
            # A file, not the default ":memory:". In-memory SQLite runs in
            # shared-cache mode, which takes coarse table locks and cannot honour
            # WAL or the busy timeout — concurrent writers get "database table is
            # locked" instead of queueing. That makes the booking concurrency
            # guarantee untestable in memory. The file is created and dropped per
            # run and costs a fraction of a second.
            "TEST": {"NAME": BASE_DIR / ".test_db.sqlite3"},
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
# Hostel wall clock (Goa / India). Slot hours, localdate(), and "past" overlays
# use this; datetimes are still stored UTC-aware in the database.
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "base.BaseUser"

# Full regression suite lives in tests/. See tests/README.md.
TEST_RUNNER = "core.test_runner.LundriiTestRunner"

# ---------------------------------------------------------------------------
# Cache — Django built-ins only, no external cache server.
#
# OTPs, one-time links and rate limits live in the cache, so a single-process
# LocMemCache is only safe when there is exactly one worker. Set
# CACHE_BACKEND=db (the production default) to use Django's DatabaseCache, which
# is shared across workers and needs no service beyond the existing database.
# The cache table is created by base/migrations/0002_cache_table.py.
# ---------------------------------------------------------------------------
CACHE_TABLE_NAME = "lundrii_cache"

CACHE_BACKEND = os.getenv("CACHE_BACKEND", "locmem" if DEBUG else "db").strip().lower()

if CACHE_BACKEND == "db":
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": CACHE_TABLE_NAME,
            # Auth entries are small; keep plenty of headroom before culling.
            "OPTIONS": {"MAX_ENTRIES": 10000, "CULL_FREQUENCY": 3},
        }
    }
elif CACHE_BACKEND == "dummy":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "lundrii-local",
        }
    }

# ---------------------------------------------------------------------------
# Background tasks — Django's built-in Tasks framework (django.tasks), no Celery.
#
# ImmediateBackend runs an enqueued Task inline, right where it is enqueued.
# That keeps a single `manage.py runserver` self-contained: no worker process,
# no broker. Tasks are still declared with @task so slow work (email, fan-out)
# is isolated, failures are swallowed instead of breaking the request, and
# swapping in a queue backend later is a settings-only change.
# ---------------------------------------------------------------------------
TASKS = {
    "default": {
        "BACKEND": os.getenv(
            "TASKS_BACKEND",
            "django.tasks.backends.immediate.ImmediateBackend",
        ),
    }
}

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if o.strip()
]

# Vercel production + preview URLs (project.vercel.app, *-git-*-team.vercel.app, …).
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://[\w.-]+\.vercel\.app$",
]

# Student/web clients stamp X-Client-Platform so bookings record `website`.
# Without this, the browser preflight for register/login fails even when the
# origin is allowed.
CORS_ALLOW_HEADERS = (
    *default_headers,
    "x-client-platform",
)

# Django CSRF checks Origin; reuse the student-app origins from CORS_ORIGINS.
CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)
if _railway_public:
    _railway_origin = f"https://{_railway_public}"
    if _railway_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_railway_origin)
for _host in ALLOWED_HOSTS:
    if _host in ("*",) or _host.startswith("."):
        continue
    _origin = f"{'http' if DEBUG else 'https'}://{_host}"
    if _origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_origin)

# Railway (and similar) terminate TLS at the proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True

# ---------------------------------------------------------------------------
# DRF
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "base.pagination.StandardPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "base.exceptions.custom_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=int(os.getenv("JWT_ACCESS_MINUTES", "60"))
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=int(os.getenv("JWT_REFRESH_DAYS", "7"))
    ),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "UPDATE_LAST_LOGIN": False,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "TOKEN_TYPE_CLAIM": "token_type",
    "JTI_CLAIM": "jti",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Lundrii API",
    # Shared with the MCP server so developers and assistants read the same
    # rules — see base/apidocs.py.
    "DESCRIPTION": API_DESCRIPTION,
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
    "TAGS": [
        {
            "name": "Auth",
            "description": "OTP login, registration, email verification, password reset.",
        },
        {
            "name": "Student",
            "description": "Mobile student APIs: me, availability, bookings, exchanges, tickets, notifications.",
        },
        {
            "name": "Admin",
            "description": "Web portal CRUD: institutes, hostels, machines, rules, students, tickets.",
        },
    ],
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "core.spectacular.tag_by_path",
    ],
    # Enum components are named after the field they came from, so the two
    # different choice sets both exposed as "status" collide and get an
    # auto-generated suffix ("StatusE46Enum") that changes whenever the schema
    # is reshuffled. Name them explicitly so clients get a stable contract.
    "ENUM_NAME_OVERRIDES": {
        "ExchangeStatusEnum": "laundry.models.ExchangeStatus.choices",
        "TicketStatusEnum": "laundry.models.TicketStatus.choices",
    },
}

# ---------------------------------------------------------------------------
# Email / Resend
# ---------------------------------------------------------------------------
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
# RESEND_FROM_EMAIL is the alias used in some env files; EMAIL_FROM wins if both are set.
# The From domain must be verified in Resend or sends are rejected.
_email_from = os.getenv("EMAIL_FROM", "").strip() or os.getenv("RESEND_FROM_EMAIL", "").strip()
EMAIL_FROM = _email_from or "Lundrii <noreply@lundrii.app>"
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "").strip()
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
# Public origin of this API as ChatGPT/Claude will call it (scheme + host, no
# trailing slash). When set, Profile's mcpUrl uses it instead of the request
# host — needed behind a proxy whose Host header is not the public URL.
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "").strip().rstrip("/")

# Auth cache TTLs / limits (OTP + one-time links live in CACHES, not DB)
OTP_TTL_SECONDS = int(os.getenv("OTP_TTL_SECONDS", "600"))  # login OTP, 10 min
OTP_VERIFY_TTL_SECONDS = int(os.getenv("OTP_VERIFY_TTL_SECONDS", "1800"))  # 30 min
OTP_RESET_TTL_SECONDS = int(os.getenv("OTP_RESET_TTL_SECONDS", "3600"))  # 1 h
VERIFY_LINK_TTL_SECONDS = int(os.getenv("VERIFY_LINK_TTL_SECONDS", "1800"))  # 30 min
RESET_LINK_TTL_SECONDS = int(os.getenv("RESET_LINK_TTL_SECONDS", "3600"))  # 1 h
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
OTP_RATE_LIMIT_MAX = int(os.getenv("OTP_RATE_LIMIT_MAX", "5"))
OTP_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("OTP_RATE_LIMIT_WINDOW_SECONDS", "900"))
OTP_COOLDOWN_SECONDS = int(os.getenv("OTP_COOLDOWN_SECONDS", "60"))

# ---------------------------------------------------------------------------
# Cloudinary (ticket photos)
# ---------------------------------------------------------------------------
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "").strip()
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()
CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "lundrii").strip() or "lundrii"
