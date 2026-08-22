# Lundrii API — Railway deploy

Set the Railway service **root directory** to `backend` so `Dockerfile` and `railway.toml` are at the build root.

## How it starts

1. Docker image: Python 3.12-slim, `pip install -r requirements.txt`.
2. `db.sqlite3` is **copied into the image** at build time (`COPY . .` in `Dockerfile`). Commit and push schema + data before each deploy you want reflected in production.
3. Container `CMD` is `./start.sh` → gunicorn only (no migrations on boot).
4. `collectstatic` runs at **image build** time (see `Dockerfile`). Railway injects `PORT`. Default **1 worker** (`WEB_CONCURRENCY`) — SQLite does not handle multiple writers well.

Health check: `GET /health/` (see `railway.toml`).

If the public URL returns **502 Application failed to respond**, the container never started gunicorn — check Railway deploy logs. The most common cause is a placeholder `SECRET_KEY` while using `core.settings.prod` (`DEBUG=False`).

## SQLite (production database)

**Do not set `DATABASE_URL` on Railway.** With no `DATABASE_URL`, Django uses `backend/db.sqlite3`.

1. Keep `db.sqlite3` **committed** (see `.gitignore` — SQLite entries are commented out on purpose).
2. Apply schema changes locally, then commit `db.sqlite3` and redeploy so the new file is baked into the image.
3. Runtime writes persist until the container is replaced; a **redeploy rebuilds from git**, so commit the DB file before deploying if you need those writes kept.

`CACHE_BACKEND=db` stores OTP/rate-limit data in the same SQLite file (cache table must exist in `db.sqlite3`).

## Environment variables

Do not put real secrets in this file. Generate `SECRET_KEY` yourself and paste it only in Railway.

| Variable | Production |
|---|---|
| `SECRET_KEY` | Required. Must not be a placeholder (`change-me-…` / Django insecure default). |
| `CACHE_BACKEND` | `db` |
| `RESEND_API_KEY` | Resend API key. |
| `EMAIL_FROM` | Verified Resend from-address, e.g. `Lundrii <noreply@lundrii.app>`. |
| `CLOUDINARY_CLOUD_NAME` | Cloudinary cloud name. |
| `CLOUDINARY_API_KEY` | Cloudinary API key. |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret. |
| `CLOUDINARY_FOLDER` | Optional; default `lundrii`. |
| `CLOUDINARY_URL` | Optional instead of the three Cloudinary vars. |

**Do not set:** `DATABASE_URL`, `ALLOWED_HOSTS`, `CORS_ORIGINS`, `FRONTEND_URL` — hosts/CORS/frontend URL are in `core/settings/prod.py`; DB is SQLite without `DATABASE_URL`.

Optional: `WEB_CONCURRENCY` (default `1` for SQLite), `TASKS_BACKEND`, JWT/OTP tunables (see `.env.example`).

Any `https://*.vercel.app` origin is already allowed for CORS via regex in `core/settings/base.py`.

## Proxy / TLS

Railway terminates HTTPS. Settings already set `SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST`, and (in `core/settings/prod.py`) `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, and `CSRF_COOKIE_SECURE`.
