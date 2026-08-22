# Lundrii API — Railway deploy

Set the Railway service **root directory** to `backend` so `Dockerfile` and `railway.toml` are at the build root.

## How it starts

1. Docker image: Python 3.12-slim, `pip install -r requirements.txt`.
2. Container `CMD` is `./start.sh`:
   - `python manage.py migrate --noinput`
   - `python manage.py check --deploy`
   - `gunicorn core.wsgi:application --bind 0.0.0.0:$PORT`
3. `collectstatic` runs at **image build** time (see `Dockerfile`). Railway injects `PORT`. Workers: `WEB_CONCURRENCY` (default `2`).

Health check: `GET /health/` (see `railway.toml`).

If the public URL returns **502 Application failed to respond**, the container never started gunicorn — check Railway deploy logs. The most common cause is a placeholder `SECRET_KEY` while using `core.settings.prod` (`DEBUG=False`).

## Postgres

Add Railway Postgres and set `DATABASE_URL` to the plugin’s connection URL (`postgres://` or `postgresql://`). With that var, Django uses PostgreSQL. Without it (local/tests), SQLite is used.

The OTP/rate-limit cache table is created by migrations (`base/migrations/0002_cache_table.py`). Keep `CACHE_BACKEND=db` in production.

## Environment variables

Do not put real secrets in this file. Generate `SECRET_KEY` yourself and paste it only in Railway.

| Variable | Production |
|---|---|
| `SECRET_KEY` | Required. Must not be a placeholder (`change-me-…` / Django insecure default). |
| `DATABASE_URL` | Railway Postgres URL. |
| `CACHE_BACKEND` | `db` |
| `RESEND_API_KEY` | Resend API key. |
| `EMAIL_FROM` | Verified Resend from-address, e.g. `Lundrii <noreply@lundrii.app>`. |
| `CLOUDINARY_CLOUD_NAME` | Cloudinary cloud name. |
| `CLOUDINARY_API_KEY` | Cloudinary API key. |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret. |
| `CLOUDINARY_FOLDER` | Optional; default `lundrii`. |
| `CLOUDINARY_URL` | Optional instead of the three Cloudinary vars. |

Optional: `WEB_CONCURRENCY`, `TASKS_BACKEND`, JWT/OTP tunables (see `.env.example`).

`ALLOWED_HOSTS`, CORS origins, and `FRONTEND_URL` are hardcoded in `core/settings/prod.py` — edit that file and redeploy to change them, not Railway env vars. Any `https://*.vercel.app` origin is already allowed for CORS via regex in `core/settings/base.py`. CSRF trusted origins are derived from the hardcoded lists in prod settings.

## Proxy / TLS

Railway terminates HTTPS. Settings already set `SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST`, and (in `core/settings/prod.py`) `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, and `CSRF_COOKIE_SECURE`.
