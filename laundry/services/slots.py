"""
Derive bookable slots from a machine's operating window + slot length.

Slots are not stored. Overlay active bookings and (optionally) the viewing
student's fairness rules to produce UI states:

    free | taken | mine | blocked | offline | past | running
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from django.utils import timezone

from laundry.models import Booking, Hostel, InstituteRule, Machine, MachineKind, Student
from laundry.services.rules import (
    RULE_COOLDOWN,
    RULE_QUOTA,
    check_booking_rules,
    get_institute_rules,
)

SLOT_FREE = "free"
SLOT_TAKEN = "taken"
SLOT_MINE = "mine"
SLOT_BLOCKED = "blocked"
SLOT_OFFLINE = "offline"
SLOT_PAST = "past"
SLOT_RUNNING = "running"

MACHINE_FREE = "free"
MACHINE_BUSY = "busy"
MACHINE_OFFLINE = "offline"

_DEFAULT_SLOT_LABELS = {
    SLOT_FREE: "Available",
    SLOT_MINE: "Your booking",
    SLOT_TAKEN: "Taken",
    SLOT_RUNNING: "Running now",
    SLOT_BLOCKED: "Blocked",
    SLOT_OFFLINE: "Machine offline",
    SLOT_PAST: "Past",
}


@dataclass(frozen=True)
class DerivedSlot:
    starts_at: datetime
    ends_at: datetime
    state: str
    hour: int
    label: str | None = None
    holder_id: UUID | None = None
    holder_name: str | None = None
    blocked_rule: str | None = None
    clears_at: datetime | None = None
    is_mine: bool = False
    booking_id: UUID | None = None


def _aware(dt: datetime) -> datetime:
    if timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt, timezone.get_current_timezone())


def _local(dt: datetime) -> datetime:
    return timezone.localtime(_aware(dt))


def iter_operating_slots(machine: Machine, on_date: date):
    """
    Yield (starts_at, ends_at) aware datetimes for ``on_date``.

    ``operating_window_start == operating_window_end`` means 24-hour operation.
    If start > end the window wraps midnight into the next calendar day.
    """
    tz = timezone.get_current_timezone()
    length = timedelta(minutes=max(int(machine.slot_length_minutes or 60), 1))
    start_t = machine.operating_window_start
    end_t = machine.operating_window_end

    day_start = timezone.make_aware(datetime.combine(on_date, start_t), tz)
    if start_t == end_t:
        end_bound = day_start + timedelta(days=1)
    elif start_t < end_t:
        end_bound = timezone.make_aware(datetime.combine(on_date, end_t), tz)
    else:
        end_bound = timezone.make_aware(
            datetime.combine(on_date + timedelta(days=1), end_t),
            tz,
        )

    current = day_start
    # Guard against pathological tiny windows / huge slot lengths.
    safety = 0
    while current + length <= end_bound and safety < 512:
        yield current, current + length
        current += length
        safety += 1


def resolve_slot(
    machine: Machine,
    starts_at: datetime,
) -> tuple[datetime, datetime] | None:
    """Return the derived (start, end) that matches ``starts_at``, or None."""
    instant = _aware(starts_at).replace(microsecond=0)
    local_date = _local(instant).date()
    for day in (local_date - timedelta(days=1), local_date, local_date + timedelta(days=1)):
        for start, end in iter_operating_slots(machine, day):
            if start == instant:
                return start, end
    return None


def slot_from_date_hour(
    machine: Machine,
    on_date: date,
    hour: int,
) -> tuple[datetime, datetime] | None:
    """Resolve a convenience date+hour (0–23, institute-local) to a derived slot."""
    tz = timezone.get_current_timezone()
    try:
        naive = datetime.combine(on_date, datetime.min.time().replace(hour=int(hour)))
    except ValueError:
        return None
    candidate = timezone.make_aware(naive, tz)
    exact = resolve_slot(machine, candidate)
    if exact:
        return exact
    for start, end in iter_operating_slots(machine, on_date):
        if _local(start).hour == int(hour):
            return start, end
    return None


def load_overlapping_bookings(
    machines,
    range_start: datetime,
    range_end: datetime,
) -> list[Booking]:
    machine_ids = [m.pk for m in machines]
    if not machine_ids:
        return []
    return list(
        Booking.objects.filter(
            machine_id__in=machine_ids,
            is_active=True,
            cancelled_at__isnull=True,
            starts_at__lt=range_end,
            ends_at__gt=range_start,
        ).select_related("student", "machine")
    )


def _booking_covering(bookings: list[Booking], machine_id, start: datetime, end: datetime):
    for booking in bookings:
        if booking.machine_id != machine_id:
            continue
        if booking.starts_at < end and booking.ends_at > start:
            return booking
    return None


def _holder_initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return f"{parts[0][0].upper()}."
    return f"{parts[0][0].upper()}.{parts[-1][0].upper()}."


def derive_slots(
    machine: Machine,
    on_date: date,
    *,
    student: Student | None = None,
    now: datetime | None = None,
    bookings: list[Booking] | None = None,
    rules: InstituteRule | None = None,
) -> list[DerivedSlot]:
    """Build the day's slot grid for ``machine``, overlaying bookings and rules."""
    now = now or timezone.now()
    now = _aware(now)
    if bookings is None:
        window_start = timezone.make_aware(
            datetime.combine(on_date - timedelta(days=1), datetime.min.time()),
            timezone.get_current_timezone(),
        )
        window_end = timezone.make_aware(
            datetime.combine(on_date + timedelta(days=2), datetime.min.time()),
            timezone.get_current_timezone(),
        )
        bookings = load_overlapping_bookings([machine], window_start, window_end)

    if student is not None and rules is None:
        rules = get_institute_rules(student.institute)

    slots: list[DerivedSlot] = []
    for start, end in iter_operating_slots(machine, on_date):
        hour = _local(start).hour
        covering = _booking_covering(bookings, machine.pk, start, end)
        is_mine = bool(
            student and covering and covering.student_id == student.pk
        )

        if now >= end:
            state = SLOT_PAST
        elif start <= now < end:
            state = SLOT_RUNNING
        elif machine.is_offline:
            state = SLOT_OFFLINE
        elif covering is not None:
            state = SLOT_MINE if is_mine else SLOT_TAKEN
        else:
            state = SLOT_FREE

        blocked_rule = None
        clears_at = None
        if (
            state == SLOT_FREE
            and student is not None
            and rules is not None
        ):
            block = check_booking_rules(
                student,
                machine,
                start,
                end,
                now=now,
                rules=rules,
            )
            if block is not None and block.rule in (RULE_QUOTA, RULE_COOLDOWN):
                state = SLOT_BLOCKED
                blocked_rule = block.rule
                clears_at = block.clears_at

        label = _DEFAULT_SLOT_LABELS[state]
        if state == SLOT_BLOCKED and blocked_rule == RULE_COOLDOWN:
            label = "Cooldown"
        elif state == SLOT_BLOCKED and blocked_rule == RULE_QUOTA:
            label = "Quota"
        elif state in (SLOT_TAKEN, SLOT_RUNNING) and covering is not None:
            initials = _holder_initials(covering.student.name)
            if state == SLOT_RUNNING and initials:
                label = f"Running now · {initials}"
            elif state == SLOT_TAKEN and covering.student.name:
                label = covering.student.name

        slots.append(
            DerivedSlot(
                starts_at=start,
                ends_at=end,
                state=state,
                hour=hour,
                label=label,
                holder_id=covering.student_id if covering else None,
                holder_name=covering.student.name if covering else None,
                blocked_rule=blocked_rule,
                clears_at=clears_at,
                is_mine=is_mine,
                booking_id=covering.pk if covering else None,
            )
        )
    return slots


def open_future_slots(
    machine: Machine,
    on_date: date,
    *,
    student: Student | None = None,
    now: datetime | None = None,
    bookings: list[Booking] | None = None,
    rules: InstituteRule | None = None,
) -> list[DerivedSlot]:
    return [
        slot
        for slot in derive_slots(
            machine,
            on_date,
            student=student,
            now=now,
            bookings=bookings,
            rules=rules,
        )
        if slot.state == SLOT_FREE
    ]


def machine_live_status(
    machine: Machine,
    *,
    now: datetime | None = None,
    bookings: list[Booking] | None = None,
    student: Student | None = None,
    rules: InstituteRule | None = None,
) -> dict:
    """
    Snapshot used by machine cards and /availability/now.

    Status: offline | busy (a booking covers now) | free.
    """
    now = _aware(now or timezone.now())
    today = _local(now).date()
    if bookings is None:
        day_start = timezone.make_aware(
            datetime.combine(today, datetime.min.time()),
            timezone.get_current_timezone(),
        )
        bookings = load_overlapping_bookings(
            [machine],
            day_start,
            day_start + timedelta(days=2),
        )

    today_slots = derive_slots(
        machine,
        today,
        student=student,
        now=now,
        bookings=bookings,
        rules=rules,
    )

    running = next((s for s in today_slots if s.state == SLOT_RUNNING), None)
    open_today = sum(
        1
        for s in today_slots
        if s.state == SLOT_FREE and s.starts_at > now
    )

    if machine.is_offline:
        status = MACHINE_OFFLINE
    elif running is not None and not running.is_mine:
        status = MACHINE_BUSY
    elif running is not None:
        # Own cycle in progress — machine is occupied.
        status = MACHINE_BUSY
    else:
        status = MACHINE_FREE

    next_taken = next(
        (
            s
            for s in today_slots
            if s.starts_at > now and s.state in (SLOT_TAKEN, SLOT_MINE)
        ),
        None,
    )
    next_claim = next(
        (s for s in today_slots if s.state == SLOT_FREE and s.starts_at > now),
        None,
    )

    frees_at = running.ends_at if running else None
    free_until = next_taken.starts_at if next_taken else None
    if status == MACHINE_FREE and not next_taken and today_slots:
        last = today_slots[-1]
        if last.ends_at > now:
            free_until = last.ends_at

    if status == MACHINE_OFFLINE:
        subtitle = "Taken out of service by the committee"
    elif status == MACHINE_BUSY and frees_at is not None:
        subtitle = (
            f"Frees at {_local(frees_at):%H:%M} · {open_today} slots open today"
        )
    elif free_until is not None:
        subtitle = (
            f"Free until {_local(free_until):%H:%M} · {open_today} slots open today"
        )
    else:
        subtitle = f"{open_today} slots open today"

    return {
        "status": status,
        "subtitle": subtitle,
        "open_slots_today": open_today,
        "running_until": running.ends_at if running else None,
        "free_until": free_until if status == MACHINE_FREE else None,
        "frees_at": frees_at,
        "next_slot_starts_at": next_claim.starts_at if next_claim else None,
        "slots": today_slots,
    }


def hostel_availability_now(
    hostel: Hostel,
    *,
    student: Student | None = None,
    now: datetime | None = None,
) -> dict:
    now = _aware(now or timezone.now())
    machines = list(
        Machine.objects.filter(hostel=hostel, is_active=True).select_related("hostel")
    )
    today = _local(now).date()
    day_start = timezone.make_aware(
        datetime.combine(today, datetime.min.time()),
        timezone.get_current_timezone(),
    )
    bookings = load_overlapping_bookings(
        machines,
        day_start,
        day_start + timedelta(days=2),
    )
    rules = get_institute_rules(student.institute) if student else None

    machine_payloads = []
    for machine in machines:
        live = machine_live_status(
            machine,
            now=now,
            bookings=bookings,
            student=student,
            rules=rules,
        )
        machine_payloads.append(
            {
                "machine": machine,
                **live,
            }
        )

    def _kind_summary(kind: str) -> dict:
        group = [p for p in machine_payloads if p["machine"].kind == kind]
        total = len(group)
        free_now = sum(1 for p in group if p["status"] == MACHINE_FREE)
        next_free = None
        for payload in group:
            starts = payload["next_slot_starts_at"]
            if starts is None:
                continue
            if next_free is None or starts < next_free["starts_at"]:
                next_free = {
                    "starts_at": starts,
                    "machine": payload["machine"],
                }
        freeing = []
        for payload in group:
            if payload["status"] != MACHINE_BUSY:
                continue
            when = payload["frees_at"] or payload["next_slot_starts_at"]
            if when is None:
                continue
            freeing.append({"machine": payload["machine"], "at": when})
        freeing.sort(key=lambda row: row["at"])
        return {
            "free_now": free_now,
            "total": total,
            "next_free_at": next_free["starts_at"] if next_free else None,
            "next_free_machine": next_free["machine"] if next_free else None,
            "freeing_soon": freeing[:5],
        }

    return {
        "as_of": now,
        "washers": _kind_summary(MachineKind.WASHER),
        "dryers": _kind_summary(MachineKind.DRYER),
        "machines": machine_payloads,
    }
