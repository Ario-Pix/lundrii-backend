import os

from dotenv import load_dotenv

load_dotenv()

from .base import *  # noqa: F403, E402

DEBUG = True
SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-dev-only-change-me")

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "10.0.2.2", "0.0.0.0"]
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
FRONTEND_URL = "http://localhost:3000"

CACHE_BACKEND = os.getenv("CACHE_BACKEND", "locmem")
CACHES = build_caches(CACHE_BACKEND)
CSRF_TRUSTED_ORIGINS = build_csrf_trusted_origins(
    cors_origins=CORS_ALLOWED_ORIGINS,
    frontend_url=FRONTEND_URL,
    allowed_hosts=ALLOWED_HOSTS,
    use_https=False,
)
SIMPLE_JWT["SIGNING_KEY"] = SECRET_KEY
