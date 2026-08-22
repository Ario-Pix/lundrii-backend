# Lundrii API smoke checklist

Prefix: `/api/v1`. Auth: `Authorization: Bearer <access>`. Schema: `/api/schema/` · Swagger: `/api/docs/`.

OTP codes print to the runserver console when `DEBUG=True` and `RESEND_API_KEY` is unset.

| Flutter screen | Exercise | Endpoint |
|---|---|---|
| Sign up | Register + domain reject | `POST /auth/register` |
| Verify email | Link token **or** email+OTP; resend | `POST /auth/verify-email`, `POST /auth/resend-verification` |
| Sign in (student) | Email + password → JWT | `POST /auth/login` `{email, password}` — **students only**; admins rejected |
| Sign in (admin OTP) | Request code → verify → JWT | `POST /auth/login/request-otp`, `POST /auth/login/verify-otp` — **admin roles only**; students get an opaque message |
| Forgot / reset | Link token **or** email+OTP+password | `POST /auth/forgot-password`, `POST /auth/reset-password` |
| Sign out | Blacklist refresh | `POST /auth/logout` · refresh `POST /auth/refresh` |
| Home · availability | Hostel switch + free-now | `GET /me/hostels`, `GET /hostels/{id}/availability/now` |
| Book · machine list | Washers / dryers | `GET /hostels/{id}/machines?kind=` |
| Machine detail / offline | Card + offline state | `GET /machines/{id}` |
| Day · 24 slots | Date grid | `GET /machines/{id}/slots?date=YYYY-MM-DD` |
| Confirm booking | Multi-item (washer+dryer); partial ok | `POST /bookings` `{ items: [...] }` |
| All busy | Record miss | `POST /availability/misses` |
| Rule blocked / unverified / suspended | Error `code` + `clearsAt` | same `POST /bookings` |
| Bookings list | Upcoming / past | `GET /bookings?status=upcoming\|past` |
| Booking detail | Single | `GET /bookings/{id}` |
| Cancel / late cancel | Free vs quota-counting | `POST /bookings/{id}/cancel` |
| Move booking | Options then move | `GET /bookings/{id}/move-options`, `POST /bookings/{id}/move` |
| Exchanges | List in/out, request/swap | `GET/POST /exchanges` |
| Exchange actions | Approve / reject / withdraw | `POST /exchanges/{id}/approve\|reject\|withdraw` |
| Tickets | List + machine-not-working (no thread) | `GET /tickets`, `GET /tickets/{id}` — `status`, `note`/`studentNote`, `photoUrl`, machine, timestamps |
| Tickets · raise | Multipart create | `POST /tickets` form-data: `note` (required), `machineId`, optional `kind=maintenance`, optional `photo`. Photos land under `lundrii/tickets/{uuid}`. Missing Cloudinary env → `CLOUDINARY_NOT_CONFIGURED` (omit photo in local smoke). Live: `manage.py ensure_cloudinary_folder` then `manage.py upload_test_photo`. |
| Profile | Standing, quota, strikes | `GET /me` · patch name/phone `PATCH /me` |
| Profile · AI providers | ChatGPT / Claude connection status | `GET /me/assistant-connections` → `mcpUrl` + per-provider `status` (`connected` while a live OAuth **refresh** grant exists, not the 1-hour access token) |
| Profile · disconnect | Revoke that product's OAuth grant | `DELETE /me/assistant-connections/chatgpt` or `…/claude` (refresh chain + OAuth access tokens; paste `lmcp_` tokens stay) |
| Institute rules | Quota / cooldown / cutoff | `GET /me/institute` |
| Activity · notifications | List, read one, read all | `GET /notifications`, `POST /notifications/{id}/read`, `POST /notifications/read-all` |
| Notification prefs | Get / update | `GET/PUT /notifications/preferences` |

## MCP connector (ChatGPT / Claude)

Student mints a token, pastes it into the assistant, then books by chat.

| Step | Exercise | Endpoint |
|---|---|---|
| Mint token | Returns plaintext **once** | `POST /api/v1/me/mcp-tokens` `{name}` → `{token: "lmcp_…"}` |
| List tokens | Hints only, never the secret | `GET /api/v1/me/mcp-tokens` |
| Revoke | Kills it immediately | `DELETE /api/v1/me/mcp-tokens/{id}` |
| Connect | JSON-RPC handshake | `POST /mcp/` `Authorization: Bearer lmcp_…` |

### Two ways to connect

**Pasted token** (Claude Code, `mcp-remote`, anything that takes a header):
mint one above and paste it. Long lived until revoked.

**OAuth 2.1** (claude.ai, ChatGPT — their connector UIs have no "paste a
secret" field, so this is the only path there). The connector drives it:

| Step | Endpoint |
|---|---|
| Find the auth server | `GET /.well-known/oauth-protected-resource` |
| Read its endpoints | `GET /.well-known/oauth-authorization-server` |
| Register itself | `POST /oauth/register` (RFC 7591, no client secret) |
| Student signs in + approves | `GET`/`POST /oauth/authorize` |
| Exchange code for tokens | `POST /oauth/token` (PKCE **S256 required**) |
| Renew | `POST /oauth/token` `grant_type=refresh_token` |

Access tokens last 1 hour; refresh tokens rotate on every use. Replaying a used
authorization code or a rotated refresh token is treated as theft — the whole
grant is revoked, access tokens included. An unauthenticated `POST /mcp/`
returns `401` with `WWW-Authenticate: Bearer resource_metadata="…"`, which is how
a connector discovers where to start.

Smoke the connector with `curl` (all four are `POST /mcp/` with the bearer header):

```bash
curl -s -X POST http://127.0.0.1:8000/mcp/ \
  -H "Authorization: Bearer $LUNDRII_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

| Tool | Arguments |
|---|---|
| `find_available_slots` | `date` (`YYYY-MM-DD`/`today`/`tomorrow`), `kind` (`washer`\|`dryer`), `hostel`, `after`, `before` |
| `book_slot` | `machine_id` (required), plus `date`+`hour` **or** `starts_at` |
| `list_my_bookings` | — |
| `cancel_booking` | `booking_id` |

Institute rules are enforced server-side: a blocked booking returns
`isError: true` with the reason (`RULE_BLOCKED`, `SLOT_TAKEN`, `SUSPENDED`,
`UNVERIFIED`, `MACHINE_OFFLINE`), never a silent failure.

## Admin portal (not Flutter)

Prefix: `/api/v1/admin/`. JWT from admin OTP login.

Existing CRUD: institutes, hostels, machines, rules, students, tickets.

### New this phase

| Area | Exercise | Endpoint |
|---|---|---|
| Profile | Read / patch display name | `GET/PATCH /admin/me/` · alias `GET/PATCH /admin/profile/` |
| Machines | Operating window | `PATCH /admin/machines/{id}/hours/` `{operating_window_start, operating_window_end}` |
| Machines | Bring online | `POST /admin/machines/{id}/online/` |
| Students | Create | `POST /admin/students/` `{name, email, phone?, hostel, gender}` |
| Students | Bulk CSV import | `POST /admin/students/import/` multipart `file` (or `csv`) → created/skipped/errors |
| Students | Promote to administrator | `POST /admin/students/{id}/promote/` |
| Students | Send password reset | `POST /admin/students/{id}/send-reset-link/` |
| Students | Booking history | `GET /admin/students/{id}/bookings/` |
| Strikes | Revoke (soft) | `POST /admin/strikes/{id}/revoke/` (or `DELETE /admin/strikes/{id}/`) |
| Suspensions | Active list | `GET /admin/suspensions/` |
| Institutes | Allowed email domains | `PATCH /admin/institutes/{id}/` `{allowed_email_domains: [...]}` (also via rules write-through) |
| Bookings grid | Day cells | `GET /admin/bookings/grid?date=&hostel=&channel=` |
| Bookings | Detail | `GET /admin/bookings/{id}/` |
| Bookings | Cancel | `POST /admin/bookings/{id}/cancel/` |
| Bookings | Day CSV | `GET /admin/bookings/export.csv?date=&hostel=` |
| Analytics | Demand by hour | `GET /admin/analytics/demand-by-hour?hostel=` |
| Analytics | Weekday shape | `GET /admin/analytics/weekday-shape?hostel=` |
| Analytics | Channel shares | `GET /admin/analytics/channel-shares` (bookings default `channel=app`) |

### Portal operations

| Area | Exercise | Endpoint |
|---|---|---|
| Dashboard | Headline numbers for a day | `GET /admin/dashboard/summary?date=&hostel=` |
| Dashboard | What needs attention, worst first | `GET /admin/dashboard/attention?hostel=` |
| Activity | Bookings + cancellations + admin actions, merged | `GET /admin/activity?hostel=&limit=` |
| Audit log | Every admin action; append-only | `GET /admin/audit-log?administrator=&action=&date_from=&date_to=` |
| Machines | **Preview** what going offline would cancel | `GET /admin/machines/{id}/offline-impact` |
| Machines | **Preview** what narrower hours would strand | `POST /admin/machines/{id}/hours-impact` `{operating_window_start, operating_window_end}` |
| Machines | Change hours; strand or cancel explicitly | `PATCH /admin/machines/{id}/hours/` `{…, cancel_outside: bool}` |
| Profile | Change own password | `POST /admin/me/change-password` `{current_password, new_password}` |

Destructive machine changes are two-step on purpose. `offline-impact` and
`hours-impact` change nothing and return the exact bookings the action would
destroy, so the portal can show them first. `PATCH hours` **keeps** stranded
bookings unless `cancel_outside: true` is sent — a student's booking is never
destroyed as a side effect of an hours edit.

## Booking integrity

A machine can never be held by two students at once. Both the create and move
paths lock the machine row, re-check for an **overlapping** live booking, and
write inside one transaction. Overlap is an interval test, not a `starts_at`
equality test: changing a machine's slot length or operating window re-cuts the
derived grid under existing bookings, so a new slot can straddle an old booking
while starting at a different minute.

Smoke it by racing two claims for one slot — exactly one should win, the other
should get `SLOT_TAKEN`:

```bash
./.venv/bin/python manage.py test tests.test_booking_concurrency
```

Demo flags stay client-only (no API).
