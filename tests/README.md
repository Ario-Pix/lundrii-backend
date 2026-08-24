# Lundrii backend test suite

Every backend API and behaviour is covered here, in one place. Run it after
every backend change — the point of the suite is that a change which breaks
something that already worked fails here, before it reaches a client.

```bash
./run_tests.sh
```

That runs Django's system checks, verifies models and migrations are in sync,
then runs everything below. Full run is a few seconds; there is no reason to
skip it.

While iterating, narrow to one module or one test:

```bash
./run_tests.sh tests.test_bookings
```

```bash
./.venv/bin/python manage.py test tests.test_auth.AuthAPITests.test_password_login_happy_path
```

## What lives where

| Module | Covers |
|---|---|
| `test_auth.py` | OTP + one-time-link services, JWT helpers, and every `/api/v1/auth/*` endpoint: register, domain allow-list, password login, admin OTP login, verify email, forgot/reset, refresh, logout blacklist |
| `test_bookings.py` | Slot generation, availability, machine listing/detail, booking create (multi-item, partial), cancel, move, move-options, and the quota / cooldown / cutoff / advance-window rule engine |
| `test_exchanges.py` | Exchange request and swap, approve, reject, withdraw, and the eligibility rules behind them |
| `test_tickets.py` | Student ticket create (machine-not-working only; conflict kind rejected), multipart photo, list, detail, S3 failure handling, and mutation permissions |
| `test_me_notifications.py` | `/me`, `/me/hostels`, `/me/institute`, standing/quota/strike display, notification list, read, read-all, and preferences |
| `test_home.py` | `GET /home` bootstrap: signed-in vs guest, washer counts, `hostelId` switch, ineligible 404, pending incoming exchanges |
| `test_admin_crud.py` | Admin portal CRUD — institutes, hostels, machines, rules, students (create, CSV import, promote, send reset link), strikes, suspensions, tickets |
| `test_admin_bookings.py` | Admin bookings grid, booking detail, admin cancel, day CSV export, and the demand / weekday / channel-share analytics |
| `test_mcp.py` | The MCP connector: token issue/revoke/expiry, bearer auth on `/mcp/`, the JSON-RPC handshake, all four booking tools, that institute rules (quota, cooldown, suspension, hostel scope) apply to a chat booking exactly as in-app, and that one student's token can never see or touch another's bookings |
| `test_mcp_oauth.py` | The OAuth 2.1 flow hosted connectors use: discovery, dynamic registration, PKCE, consent, code exchange, refresh rotation — and the attacks (replayed codes, replayed refresh tokens, mismatched clients, open-redirect attempts) |
| `test_booking_channel.py` | Where `Booking.channel` comes from: the JWT client claim, the platform header, User-Agent sniffing, and that an MCP booking cannot be disguised as an app one |
| `test_api_docs.py` | That Swagger and MCP both draw their prose from `base/apidocs.py`, and that every error code the API raises is documented |
| `test_seed_pilot.py` | The `seed_pilot` management command |
| `test_api_surface.py` | Cross-cutting guard rails: every private route rejects anonymous callers, every documented route is still registered, the OpenAPI schema builds with zero warnings and still describes every route, and the shared error / pagination envelopes keep their shape |
| `test_infra_cache.py` | The cache layer both LocMemCache and DatabaseCache must provide, the cache-table migration, and that no Redis client is installed |
| `test_infra_tasks.py` | The `django.tasks` background-task layer: task definitions, immediate and dummy backends, that views enqueue their email, that a mail outage cannot 500 a request, and that no Celery is installed |

## Conventions

**One place for tests.** Tests live in `tests/`, not in `<app>/tests.py`. A
single suite is what makes "run everything before you ship" cheap enough to
actually do.

**The runner pins the environment.** `core/test_runner.py` forces a fast
password hasher, LocMemCache, the immediate task backend, and an empty Resend
key. So the suite runs the same on every machine regardless of local `.env`,
never reaches the network, and finishes in seconds rather than minutes. Tests
that need a different backend say so with `override_settings`.

**Mock at the seam, not at the caller.** Outbound email is patched at
`base.tasks.send_*` — the boundary between the task and the mail provider.
Patching there keeps the test honest: the request still goes through the view,
through `.enqueue()`, and into the task.

## Adding coverage

When you add an endpoint or change behaviour:

1. Add its tests to the module above that owns that domain — a new file only for
   a genuinely new domain.
2. If you added a route, add its name to `test_documented_routes_are_registered`
   in `test_api_surface.py`, and update `SMOKE.md`.
3. Run `./run_tests.sh`. Everything passes, or the change is not done.

A plain `APIView` needs `@extend_schema` spelling out its `request` and
`responses`. drf-spectacular cannot introspect one on its own, and it drops
endpoints it cannot introspect rather than failing — so an undocumented view
disappears from `/api/schema/` while every other test stays green.
`test_schema_builds_without_warnings_or_errors` is what catches that.
