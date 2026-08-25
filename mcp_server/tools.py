"""
The tools an assistant can call on a student's behalf.

Every tool is a thin wrapper over the same services the mobile app uses
(`laundry.services.*`). Nothing here re-implements booking logic, so quota,
advance-window, suspension, verification and slot-collision rules
apply identically whether a booking comes from the app or from a chat. A tool
that skipped those services would be a second, divergent booking path — the one
thing this module must not become.

Tools return plain text. That is what the model actually reads, and it keeps
identifiers visible so a `find_available_slots` result can be fed straight into
`book_slot`.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta

from django.utils import timezone

from base.apidocs import MCP_TOOL_NOTES
from base.exceptions import APIError
from laundry.models import BookingChannel, MachineKind
from laundry.services.booking import (
    BookingRequest,
    cancel_booking,
    create_bookings,
    get_own_booking,
    upcoming_bookings_qs,
)
from laundry.services.rules import quota_status, visible_hostels
from laundry.services.slots import derive_slots, load_overlapping_bookings

# A chat request like "book me a washer tomorrow" should not turn into a scan of
# the whole term.
MAX_DAYS_AHEAD = 14
MAX_SLOTS_RETURNED = 40


class ToolError(Exception):
    """A tool failed for a reason the student should see in the chat."""


def _local(dt: datetime) -> datetime:
    return timezone.localtime(dt)


def _parse_date(raw: str | None, *, field: str = "date") -> date_cls:
    if raw is None or not str(raw).strip():
        return timezone.localdate()
    text = str(raw).strip().lower()
    today = timezone.localdate()
    if text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date_cls.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(
            f"`{field}` must be YYYY-MM-DD (or 'today'/'tomorrow'); got {raw!r}."
        ) from exc


def _parse_kind(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip().lower().rstrip("s")
    if text in {"washer", "washing machine", "wash"}:
        return MachineKind.WASHER
    if text in {"dryer", "drier", "dry"}:
        return MachineKind.DRYER
    raise ToolError(f"`kind` must be 'washer' or 'dryer'; got {raw!r}.")


def _parse_hour(raw) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    # Accept "14", "14:00", "2pm".
    try:
        if text.lower().endswith(("am", "pm")):
            suffix = text[-2:].lower()
            hour = int(text[:-2].strip().split(":")[0])
            if hour == 12:
                hour = 0
            return hour + (12 if suffix == "pm" else 0)
        return int(text.split(":")[0])
    except ValueError as exc:
        raise ToolError(f"Could not read a time from {raw!r}. Use an hour like 14.") from exc


def _machines_for(student, *, kind: str | None, hostel_name: str | None):
    from laundry.models import Machine

    hostels = list(visible_hostels(student))
    if hostel_name:
        wanted = hostel_name.strip().lower()
        hostels = [h for h in hostels if h.name.strip().lower() == wanted]
        if not hostels:
            raise ToolError(
                f"You don't have access to a hostel called {hostel_name!r}."
            )
    if not hostels:
        raise ToolError("No hostel is assigned to your account yet.")

    qs = Machine.objects.filter(
        hostel__in=hostels, is_active=True
    ).select_related("hostel")
    if kind:
        qs = qs.filter(kind=kind)
    return list(qs.order_by("hostel__name", "kind", "location_name"))


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def find_available_slots(
    student,
    *,
    date=None,
    kind=None,
    hostel=None,
    after=None,
    before=None,
) -> str:
    on_date = _parse_date(date)
    machine_kind = _parse_kind(kind)
    after_hour = _parse_hour(after)
    before_hour = _parse_hour(before)

    today = timezone.localdate()
    if on_date < today:
        raise ToolError(f"{on_date.isoformat()} is in the past.")
    if on_date > today + timedelta(days=MAX_DAYS_AHEAD):
        raise ToolError(
            f"Only the next {MAX_DAYS_AHEAD} days can be checked; "
            f"{on_date.isoformat()} is further out."
        )

    machines = _machines_for(student, kind=machine_kind, hostel_name=hostel)
    now = timezone.now()
    window_start = timezone.make_aware(
        datetime.combine(on_date - timedelta(days=1), datetime.min.time()),
        timezone.get_current_timezone(),
    )
    bookings = load_overlapping_bookings(
        machines, window_start, window_start + timedelta(days=3)
    )

    rows: list[tuple[datetime, str, str]] = []
    blocked_reason = None
    for machine in machines:
        for slot in derive_slots(
            machine, on_date, student=student, now=now, bookings=bookings
        ):
            if slot.state == "blocked" and blocked_reason is None:
                blocked_reason = slot.blocked_rule
            if slot.state != "free" or slot.starts_at <= now:
                continue
            hour = _local(slot.starts_at).hour
            if after_hour is not None and hour < after_hour:
                continue
            if before_hour is not None and hour >= before_hour:
                continue
            rows.append(
                (
                    slot.starts_at,
                    machine.kind,
                    f"{_local(slot.starts_at):%H:%M}–{_local(slot.ends_at):%H:%M} · "
                    f"{machine.get_kind_display()} · {machine.location_name} "
                    f"({machine.hostel.name}) · machine_id={machine.id}",
                )
            )

    if not rows:
        detail = f"No free slots on {on_date.isoformat()}"
        if machine_kind:
            detail += f" for a {machine_kind}"
        if after_hour is not None or before_hour is not None:
            detail += " in that time range"
        if blocked_reason:
            detail += (
                f". Some slots are open but blocked for you by the "
                f"{blocked_reason} rule — check `list_my_bookings` for your quota"
            )
        return detail + "."

    rows.sort(key=lambda row: (row[0], row[1]))
    shown = rows[:MAX_SLOTS_RETURNED]
    lines = [f"{len(rows)} free slot(s) on {on_date.isoformat()}:"]
    lines += [f"  - {row[2]}" for row in shown]
    if len(rows) > len(shown):
        lines.append(f"  … and {len(rows) - len(shown)} more.")
    lines.append(
        "To book one, call book_slot with its machine_id and the start hour."
    )
    return "\n".join(lines)


def book_slot(student, *, machine_id=None, date=None, hour=None, starts_at=None) -> str:
    if not machine_id:
        raise ToolError(
            "`machine_id` is required. Call find_available_slots first to get one."
        )

    request_kwargs = {"machine_id": machine_id}
    if starts_at:
        parsed = _parse_iso_datetime(starts_at)
        request_kwargs["starts_at"] = parsed
    else:
        parsed_hour = _parse_hour(hour)
        if parsed_hour is None:
            raise ToolError("Provide either `starts_at`, or `date` plus `hour`.")
        request_kwargs["date"] = _parse_date(date)
        request_kwargs["hour"] = parsed_hour

    try:
        results = create_bookings(
            student,
            [BookingRequest(**request_kwargs)],
            channel=BookingChannel.MCP,
        )
    except APIError as exc:
        # UNVERIFIED / SUSPENDED reject the whole request.
        raise ToolError(f"{exc.code}: {exc.detail}") from exc

    result = results[0]
    if not result.ok:
        message = f"Could not book that slot ({result.code}): {result.detail}"
        if result.clears_at:
            message += f" Clears at {_local(result.clears_at):%Y-%m-%d %H:%M}."
        raise ToolError(message)

    booking = result.booking
    return (
        "Booked.\n"
        f"  {booking.machine.get_kind_display()} · {booking.machine.location_name} "
        f"({booking.machine.hostel.name})\n"
        f"  {_local(booking.starts_at):%A %d %b, %H:%M}"
        f"–{_local(booking.ends_at):%H:%M}\n"
        f"  booking_id={booking.id}"
    )


def list_my_bookings(student, **_ignored) -> str:
    bookings = list(upcoming_bookings_qs(student)[:20])
    quota = quota_status(student)

    header = (
        f"Quota: {quota['used']}/{quota['limit']} bookings used this week "
        f"(Monday–Sunday)."
    )
    if not bookings:
        return f"No upcoming bookings.\n{header}"

    lines = [f"{len(bookings)} upcoming booking(s):"]
    for booking in bookings:
        lines.append(
            f"  - {_local(booking.starts_at):%A %d %b, %H:%M}"
            f"–{_local(booking.ends_at):%H:%M} · "
            f"{booking.machine.get_kind_display()} · "
            f"{booking.machine.location_name} ({booking.machine.hostel.name}) · "
            f"booking_id={booking.id}"
        )
    lines.append(header)
    return "\n".join(lines)


def cancel_my_booking(student, *, booking_id=None) -> str:
    if not booking_id:
        raise ToolError(
            "`booking_id` is required. Call list_my_bookings to find it."
        )
    try:
        booking = get_own_booking(student, booking_id)
        cancelled = cancel_booking(student, booking)
    except APIError as exc:
        raise ToolError(f"{exc.code}: {exc.detail}") from exc

    note = " This counted as a late cancellation." if cancelled.is_late_cancel else ""
    return (
        f"Cancelled your {cancelled.machine.get_kind_display().lower()} booking on "
        f"{_local(cancelled.starts_at):%A %d %b at %H:%M}.{note}"
    )


def _parse_iso_datetime(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise ToolError(
            f"`starts_at` must be an ISO timestamp like 2026-08-20T14:00; got {raw!r}."
        ) from exc
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


# --------------------------------------------------------------------------
# Registry — name -> (handler, MCP tool descriptor)
# --------------------------------------------------------------------------

TOOLS = {
    "find_available_slots": (
        find_available_slots,
        {
            "title": "Find available laundry slots",
            "description": MCP_TOOL_NOTES["find_available_slots"],
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": (
                            "Day to check as YYYY-MM-DD, or 'today'/'tomorrow'. "
                            "Defaults to today."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["washer", "dryer"],
                        "description": "Restrict to washers or dryers.",
                    },
                    "hostel": {
                        "type": "string",
                        "description": "Hostel name, if the student has more than one.",
                    },
                    "after": {
                        "type": "string",
                        "description": "Only slots starting at or after this hour, e.g. '18' or '6pm'.",
                    },
                    "before": {
                        "type": "string",
                        "description": "Only slots starting before this hour, e.g. '22'.",
                    },
                },
            },
        },
    ),
    "book_slot": (
        book_slot,
        {
            "title": "Book a laundry slot",
            "description": MCP_TOOL_NOTES["book_slot"],
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "machine_id": {
                        "type": "string",
                        "description": "Machine UUID from find_available_slots.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Day as YYYY-MM-DD, or 'today'/'tomorrow'. Defaults to today.",
                    },
                    "hour": {
                        "type": "string",
                        "description": "Start hour, e.g. '14' or '2pm'.",
                    },
                    "starts_at": {
                        "type": "string",
                        "description": (
                            "Exact ISO start time, e.g. '2026-08-20T14:00'. "
                            "Alternative to date + hour."
                        ),
                    },
                },
                "required": ["machine_id"],
            },
        },
    ),
    "list_my_bookings": (
        list_my_bookings,
        {
            "title": "List my upcoming bookings",
            "description": MCP_TOOL_NOTES["list_my_bookings"],
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
            "inputSchema": {"type": "object", "properties": {}},
        },
    ),
    "cancel_booking": (
        cancel_my_booking,
        {
            "title": "Cancel a booking",
            "description": MCP_TOOL_NOTES["cancel_booking"],
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": True,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "booking_id": {
                        "type": "string",
                        "description": "Booking UUID from list_my_bookings.",
                    },
                },
                "required": ["booking_id"],
            },
        },
    ),
}


def tool_descriptors() -> list[dict]:
    """The `tools/list` payload."""
    return [{"name": name, **descriptor} for name, (_, descriptor) in TOOLS.items()]


def call_tool(name: str, student, arguments: dict) -> str:
    handler = TOOLS.get(name)
    if handler is None:
        raise ToolError(f"Unknown tool {name!r}.")
    # Ignore unknown keys rather than erroring: models routinely invent extras,
    # and failing the call teaches them nothing useful.
    accepted = handler[1]["inputSchema"].get("properties", {})
    kwargs = {k: v for k, v in (arguments or {}).items() if k in accepted}
    return handler[0](student, **kwargs)
