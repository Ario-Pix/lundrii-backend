# Lundrii API — Railway deploy

Set the Railway service **root directory** to `backend` so `Dockerfile` and `railway.toml` are at the build root.

## How it starts

1. Docker image: Python 3.12-slim, `pip install -r requirements.txt`.
2. Container `CMD` is `./start.sh`:
   - `python manage.py migrate --noinput`
   - `python manage.py collectstatic --noinput`
   - `gunicorn core.wsgi:application --bind 0.0.0.0:$PORT`
3. Railway injects `PORT`. Workers: `WEB_CONCURRENCY` (default `2`).

Health check: `GET /api/docs/` (Swagger UI, unauthenticated).

## Postgres

Add Railway Postgres and set `DATABASE_URL` to the plugin’s connection URL (`postgres://` or `postgresql://`). With that var, Django uses PostgreSQL. Without it (local/tests), SQLite is used.

The OTP/rate-limit cache table is created by migrations (`base/migrations/0002_cache_table.py`). Keep `CACHE_BACKEND=db` in production.

## Environment variables

Do not put real secrets in this file. Generate `SECRET_KEY` yourself and paste it only in Railway.

| Variable | Production |
|---|---|
| `SECRET_KEY` | Required. Must not be a placeholder (`change-me-…` / Django insecure default). |
| `DEBUG` | `False` |
| `ALLOWED_HOSTS` | Railway hostname, e.g. `lundrii-api.up.railway.app` (comma-separated if more). |
| `CORS_ORIGINS` | Production student app origin(s), e.g. `https://your-app.vercel.app`. Also used for `CSRF_TRUSTED_ORIGINS`. Include every Vercel origin that calls the API. Preview URLs need extra entries or skip previews. |
| `FRONTEND_URL` | Production student app URL (no trailing slash). Email verify/reset links use this. |
| `DATABASE_URL` | Railway Postgres URL. |
| `CACHE_BACKEND` | `db` |
| `RESEND_API_KEY` | Resend API key. |
| `EMAIL_FROM` | Verified Resend from-address, e.g. `Lundrii <noreply@lundrii.app>`. |
| `CLOUDINARY_CLOUD_NAME` | Cloudinary cloud name. |
| `CLOUDINARY_API_KEY` | Cloudinary API key. |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret. |
| `CLOUDINARY_FOLDER` | Optional; default `lundrii`. |
| `CLOUDINARY_URL` | Optional instead of the three Cloudinary vars. |

Optional: `WEB_CONCURRENCY`, `TASKS_BACKEND`, JWT/OTP tunables (see `.env.example`). CSRF is derived from `CORS_ORIGINS` plus `https://` + each `ALLOWED_HOSTS` entry when `DEBUG=False`.

## Proxy / TLS

Railway terminates HTTPS. Settings already set `SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST`, and (when `DEBUG=False`) `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, and `CSRF_COOKIE_SECURE`.
