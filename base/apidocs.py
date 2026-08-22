"""
One source of truth for what the API means.

Two audiences read this project's documentation and they need the same facts:

* a developer in Swagger UI at `/api/docs/`, and
* a language model deciding which MCP tool to call and how to react when it
  fails.

Keeping two copies of "what does RULE_BLOCKED mean" guarantees they drift, and
the model's copy is the one nobody proofreads. So the prose lives here once,
and both surfaces import it — `laundry/views/*` feed it to drf-spectacular via
`@extend_schema`, and `mcp_server/protocol.py` and `mcp_server/tools.py` feed it
into the server instructions and tool descriptions.

Write for both readers: state the rule, then what to do about it.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# The domain, explained once
# ---------------------------------------------------------------------------

BOOKING_MODEL = """\
Slots are not stored rows. Each machine has an operating window and a slot
length, and the bookable slots for a day are derived from those. A slot is
identified by its machine and its start time — there is no slot id.
"""

FAIRNESS_RULES = """\
Every institute sets its own fairness rules, and the server enforces them on
every booking path (mobile app, web, and assistant alike):

- **Quota** — a maximum number of bookings Monday to Sunday. The count
  resets every Monday.
- **Advance window** — how far ahead bookings may be made at all.
- **Cancellation cutoff** — cancelling nearer the start than this still counts
  against quota, and is recorded as a late cancellation.

A student who is suspended or has not verified their email cannot book at all.
Read the caller's current rules from `GET /api/v1/me/institute`.
"""

SLOT_STATES = """\
Derived slots carry a state:

| State | Meaning |
|---|---|
| `free` | Bookable by this student right now |
| `taken` | Held by another student |
| `mine` | Held by this student |
| `blocked` | Nobody holds it, but a fairness rule stops *this* student booking it |
| `offline` | Machine is out of service |
| `running` | In progress at this moment |
| `past` | Already ended |

`blocked` is the one worth handling specially: the slot is genuinely empty, so
telling a student "it's taken" would be wrong. It carries the rule that blocked
it and when that clears.
"""

ERROR_ENVELOPE = """\
Every error returns the same envelope:

```json
{ "code": "RULE_BLOCKED", "detail": "Human-readable explanation." }
```

`code` is stable and safe to branch on. `detail` is written for a person and
may change. Some codes add fields — `RULE_BLOCKED` adds `rule` and `clearsAt`,
`DOMAIN_REJECTED` adds `allowedDomains`.
"""

ERROR_CODES = {
    "VALIDATION_ERROR": "The request body was malformed or missing fields.",
    "AUTHENTICATION_FAILED": "No credentials, or they were wrong or expired.",
    "PERMISSION_DENIED": "Authenticated, but not allowed to do this.",
    "NOT_FOUND": "No such object, or it belongs to someone else.",
    "RULE_BLOCKED": (
        "An institute fairness rule stopped the booking. Carries `rule` "
        "(quota | advance_window) and `clearsAt`. Retrying sooner "
        "will fail the same way — wait, or pick a time after `clearsAt`."
    ),
    "SLOT_TAKEN": (
        "Someone claimed that slot first. Slots are first-come, so re-check "
        "availability and pick another."
    ),
    "MACHINE_OFFLINE": "The machine is out of service. Choose a different one.",
    "PAST_SLOT": "That slot has already started or ended.",
    "OUTSIDE_ADVANCE_WINDOW": "Bookings that far ahead are not open yet.",
    "UNVERIFIED": "The student has not verified their email address yet.",
    "SUSPENDED": (
        "The student is suspended and cannot book until it lifts. Carries "
        "`clearsAt`."
    ),
    "INVALID_OTP": "The one-time code or link was wrong, expired, or already used.",
    "RATE_LIMITED": "Too many requests. Carries `retry_after` in seconds.",
    "DOMAIN_REJECTED": "That email domain is not on any institute's allow-list.",
    "CLOUDINARY_NOT_CONFIGURED": "Photo storage is not configured on this deployment.",
    "CLOUDINARY_UPLOAD_FAILED": "Cloudinary rejected the photo upload or was unreachable.",
    "S3_NOT_CONFIGURED": "Photo storage is not configured on this deployment. Alias of CLOUDINARY_NOT_CONFIGURED.",
}


def error_table(*codes: str) -> str:
    """Render a markdown table for the codes an endpoint can return."""
    wanted = codes or tuple(ERROR_CODES)
    rows = "\n".join(
        f"| `{code}` | {ERROR_CODES[code]} |" for code in wanted if code in ERROR_CODES
    )
    return f"**Errors**\n\n| Code | Meaning |\n|---|---|\n{rows}\n"


BOOKING_FLOW = """\
The booking flow, in order:

1. `GET /api/v1/me/hostels` — which hostels this student may book in.
2. `GET /api/v1/hostels/{id}/machines` — washers and dryers there.
3. `GET /api/v1/machines/{id}/slots?date=YYYY-MM-DD` — the day's derived slots.
4. `POST /api/v1/bookings` — claim one or more. A washer and a dryer in one
   request are two independent bookings; either can succeed while the other
   fails, so always read the per-item results.
"""

API_DESCRIPTION = f"""\
Student laundry booking for university hostels.

Authentication is a JWT bearer token from `/api/v1/auth/login` (students) or
`/api/v1/auth/login/verify-otp` (administrators). Send it as
`Authorization: Bearer <access>`.

Assistants (ChatGPT, Claude) do not use this REST API. They connect to the MCP
server at `/mcp/`, which exposes the same booking capability as a small set of
tools. See the *MCP* section of `SMOKE.md`.

## How booking works

{BOOKING_MODEL}
{BOOKING_FLOW}
## Fairness rules

{FAIRNESS_RULES}
## Slot states

{SLOT_STATES}
## Errors

{ERROR_ENVELOPE}
"""

# ---------------------------------------------------------------------------
# The same facts, addressed to a model rather than a developer
# ---------------------------------------------------------------------------

MCP_SERVER_INSTRUCTIONS = f"""\
Lundrii is a student laundry booking service. You are acting for one signed-in
student, and every tool acts only on their own data.

To book: call `find_available_slots` for the day they want, pick a slot matching
their stated preference, then call `book_slot` with that slot's `machine_id` and
hour. Never guess a `machine_id` — slots have no id of their own, so a booking
is identified by machine plus start time.

{FAIRNESS_RULES}
These rules are enforced by the server, not by you. When a call is refused you
get the reason in the response text. Tell the student that reason rather than
retrying — a booking blocked by quota will be blocked again a second
later. If it says the slot was just taken, re-check availability and offer them
what is actually free.

Confirm before you book. A booking consumes the student's quota, and cancelling
close to the start time can still count against it.
"""

MCP_TOOL_NOTES = {
    "find_available_slots": (
        "List free laundry slots the student can actually book on a given day. "
        "Returns each slot's machine_id, which book_slot needs. Slots blocked "
        "by the student's own quota are left out, because offering "
        "one would only fail. Always call this before book_slot."
    ),
    "book_slot": (
        "Book one laundry slot. Use a machine_id from find_available_slots and "
        "either date + hour, or an exact starts_at. Fails with a reason if the "
        "slot was just taken, the machine is offline, or a fairness rule blocks "
        "it."
    ),
    "list_my_bookings": (
        "The student's upcoming bookings with their booking_ids, plus how much "
        "of their Monday–Sunday quota is used. Call this before cancelling, and to "
        "answer questions about what they have booked."
    ),
    "cancel_booking": (
        "Cancel one upcoming booking, by booking_id from list_my_bookings. "
        "Cancelling nearer the start than the institute's cutoff still counts "
        "against quota and is recorded as a late cancellation — say so if the "
        "booking is soon."
    ),
}
