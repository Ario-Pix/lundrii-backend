# Lundrii backend

Django 6.1 + DRF. API prefix `/api/v1`. Schema at `/api/schema/`, Swagger at
`/api/docs/`. Endpoint inventory lives in `SMOKE.md`.

## Run the tests after every change

```bash
./run_tests.sh
```

This is not optional and not a final-polish step. Any change to backend code —
a new endpoint, a changed serializer, a bumped dependency, a one-line fix —
must end with a full green run. The suite covers every API in the project, so a
green run is the evidence that new code did not break existing behaviour. It
takes a few seconds.

`run_tests.sh` runs three gates: Django system checks, a
`makemigrations --check` so models and migrations cannot drift apart, then the
whole suite in `tests/`.

When you add or change an endpoint, add its tests in the matching module under
`tests/` and register its route name in `test_api_surface.py`. See
`tests/README.md` for the map of what is covered where.

## No external infrastructure

Two deliberate constraints. Both are covered by tests that fail if the
dependency comes back.

**Cache: Django's built-in backends only — no Redis.** OTPs, one-time
verify/reset links and rate-limit counters live in the cache, so it is
load-bearing for auth. `CACHE_BACKEND=locmem` (dev default) is in-process and
only correct with a single worker; `CACHE_BACKEND=db` (production default) uses
Django's `DatabaseCache` on the database already in use, which is shared across
workers and needs no extra service. The cache table is created by
`base/migrations/0002_cache_table.py`, so `migrate` is all that is needed.

**Background work: Django's Tasks framework — no Celery.** Outbound email is
declared with `@task` in `base/tasks.py` and enqueued with `.enqueue()`. The
default `ImmediateBackend` runs the task inline, so `runserver` stays
self-contained: no broker, no worker process. Two properties come from this
regardless of backend: a mail provider outage is captured on the `TaskResult`
instead of turning a successful registration into a 500, and moving this work
off the request cycle later is a change to the `TASKS` setting rather than to
every call site.

Django core ships no worker, so with `ImmediateBackend` a Task is *declared*
background work but still runs inside the request. If mail latency starts
showing up in response times, configure a third-party `django.tasks` backend
that provides a worker and set `TASKS_BACKEND` — the task definitions and call
sites do not change.

Task arguments are JSON-serialized, so pass primitives (emails, tokens, ids),
never model instances.

## MCP connector

`mcp_server/` lets a student connect their account to ChatGPT or Claude and book
by chat. It is an MCP server speaking JSON-RPC over Streamable HTTP at `/mcp/`,
implemented directly — no SDK, for the same reason there is no Redis or Celery.
A tools-only server is five methods, and the official SDK is ASGI/Starlette-based
which this WSGI project does not otherwise need.

Auth is a long-lived connector token (`lmcp_…`) sent as `Authorization: Bearer`,
minted by the student at `POST /api/v1/me/mcp-tokens` and revocable at
`DELETE /api/v1/me/mcp-tokens/{id}`. Only an HMAC is stored; the plaintext is
returned once. A token acts for exactly one student and reaches nothing but the
tools.

**Tools must stay thin wrappers over `laundry/services/*`.** `find_available_slots`,
`book_slot`, `list_my_bookings` and `cancel_booking` all go through the same
services the mobile app uses, so quota, cooldown, advance-window, suspension,
verification and slot-collision rules apply identically to a chat booking. A tool
that reimplemented any of that would be a second booking path that drifts from
the first — the one thing `mcp_server/tools.py` must not become.

Tool failures come back as a normal JSON-RPC result with `isError: true` and a
readable reason, not as a JSON-RPC error. The model needs to read "that slot was
just taken" and adapt; a transport-level fault tells it nothing.

## API documentation

`base/apidocs.py` is the single source of truth for what the API *means* —
fairness rules, slot states, the error-code catalogue, the booking flow. Two
surfaces import it:

* **Swagger UI** at `/api/docs/` — `SPECTACULAR_SETTINGS["DESCRIPTION"]` plus
  `@extend_schema(description=..., examples=[...])` on the student views.
* **MCP** — `protocol.SERVER_INSTRUCTIONS` and every tool description.

Write documentation there, not inline, so a developer and a model are told the
same thing. `tests/test_api_docs.py` fails if either surface stops drawing from
it, and if an error code raised by `base/exceptions.py` has no entry.

A plain `APIView` needs `@extend_schema` spelling out `request` and `responses`;
drf-spectacular silently drops views it cannot introspect. The gate catches that.

## Booking source

`Booking.channel` records where a booking came from, and **no client ever sends
it**. `base/clients.py` resolves it: an MCP-authenticated request is `mcp` (a
fact about the credential, unspoofable); otherwise the signed `client` claim
stamped into the JWT at login wins, then the `X-Client-Platform` header, then a
User-Agent guess, then `app`.

Treat it as telemetry, not a security boundary — everything but the MCP case
originates with the caller. Nothing is authorised on it.

Adding a `BookingChannel` means adding its display name to `palette_names` in
`laundry/services/analytics.py`; a name missing there is counted in the total but
never emitted, which silently drops those bookings and skews every percentage.

## Dependencies

`requirements.txt` pins lower bounds to tested versions and caps the major, so
an upgrade is a deliberate change. After any dependency change, run
`./run_tests.sh` before anything else.
