# Lundrii API — Railway deploy

Set the Railway service **root directory** to `backend` so `Dockerfile` and `railway.toml` are at the build root.

## How it starts

1. Docker image: Python 3.12-slim, `pip install -r requirements.txt`.
2. `collectstatic` runs at **image build** time (see `Dockerfile`). It does **not** need `DATABASE_URL` — production settings skip the Neon requirement when the command is `collectstatic`, so the image still builds without the database.
3. Container `CMD` is `./start.sh`: require `DATABASE_URL`, run `migrate --noinput`, then gunicorn. Railway injects `PORT`.
4. Default **1 worker** (`WEB_CONCURRENCY`). Postgres can run more than one; set `WEB_CONCURRENCY=2` (or higher) on Railway if you want parallel workers.

Health check: `GET /health/` (see `railway.toml`).

If the public URL returns **502 Application failed to respond**, the container never started gunicorn — check Railway deploy logs. Common causes: a placeholder `SECRET_KEY` while using `core.settings.prod` (`DEBUG=False`), or a missing `DATABASE_URL`.

## Postgres (Neon)

**Set `DATABASE_URL` on Railway** to the Neon `postgres://` or `postgresql://` URI (include `sslmode=require` and `channel_binding=require` as Neon provides them). Paste the URI only in Railway / local `.env` — never in this file or git.

Committed `backend/db.sqlite3` is **not** the production database. It is a local/pilot snapshot only. Runtime data lives in Neon; a redeploy does not bake SQLite into the live service.

`CACHE_BACKEND=db` stores OTP/rate-limit data in the same Postgres database (cache table is created by migrations on boot).

## Environment variables

Do not put real secrets in this file. Generate `SECRET_KEY` yourself and paste it only in Railway. Paste the Neon URI only as `DATABASE_URL` in Railway.

| Variable | Production |
|---|---|
| `SECRET_KEY` | Required. Must not be a placeholder (`change-me-…` / Django insecure default). |
| `DATABASE_URL` | Required. Neon `postgresql://…?sslmode=require&channel_binding=require`. |
| `CACHE_BACKEND` | `db` |
| `RESEND_API_KEY` | Resend API key. |
| `EMAIL_FROM` | Required. Verified Resend From on the same domain as local: `Lundrii <notifications@techconsultancycompany.com>`. Do not leave this unset (the `noreply@lundrii.app` fallback is not a verified domain). |
| `CLOUDINARY_CLOUD_NAME` | Cloudinary cloud name. |
| `CLOUDINARY_API_KEY` | Cloudinary API key. |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret. |
| `CLOUDINARY_FOLDER` | Optional; default `lundrii`. |
| `CLOUDINARY_URL` | Optional instead of the three Cloudinary vars. |

**Do not set:** `ALLOWED_HOSTS`, `CORS_ORIGINS`, `FRONTEND_URL`.

Optional: `WEB_CONCURRENCY` (default `1`; Postgres can use `2+`), `TASKS_BACKEND`, JWT/OTP tunables (see `.env.example`), `MCP_PUBLIC_URL` (defaults to `https://lundrii-backend-production.up.railway.app` so ChatGPT/Claude get a stable MCP origin).

Any `https://*.vercel.app` origin is already allowed for CORS via regex in `core/settings/base.py`.

## Proxy / TLS

Railway terminates HTTPS. Settings already set `SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST`, and (in `core/settings/prod.py`) `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, and `CSRF_COOKIE_SECURE`.
