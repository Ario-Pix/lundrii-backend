"""
Read-side helpers for the admin portal: impact previews, dashboard, activity.

The impact helpers exist because two admin actions destroy student bookings —
taking a machine offline, and narrowing its operating window. The portal spec
requires the administrator be shown what will be lost *before* committing, so
the same query that drives the preview must drive the action. Anything else and
the preview drifts from what actually happens.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from laundry.models import (
    Booking,
    Machine,
    Student,
    Ticket,
    TicketKind,
    TicketStatus,
)
from laundry.services.slots import iter_operating_slots

# How long a machine may sit offline before the portal flags it.
STALE_OFFLINE_DAYS = 3


def _aware(dt: datetime) -> datetime:
    if timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt, timezone.get_current_timezone())


def _day_bounds(on_date: date_cls) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(on_date, time.min), tz)
    return start, start + timedelta(days=1)


def upcoming_bookings_for_machine(machine: Machine, *, now=None):
    now = _aware(now or timezone.now())
    return (
        Booking.objects.filter(
            machine=machine,
            is_active=True,
            cancelled_at__isnull=True,
            ends_at__gt=now,
        )
        .select_related("student", "student__user")
        .order_by("starts_at")
    )


def _impact(bookings) -> dict:
    rows = list(bookings)
    return {
        "affected_count": len(rows),
        "students_notified": len({b.student_id for b in rows}),
        "bookings": [
            {
                "id": b.id,
                "starts_at": b.starts_at,
                "ends_at": b.ends_at,
                "student_id": b.student_id,
                "student_name": b.student.name,
                "student_email": b.student.user.email,
            }
            for b in rows
        ],
    }


def offline_impact(machine: Machine, *, now=None) -> dict:
    """Every upcoming booking that taking this machine offline would cancel."""
    return _impact(upcoming_bookings_for_machine(machine, now=now))


def hours_change_impact(
    machine: Machine,
    new_start: time,
    new_end: time,
    *,
    now=None,
    horizon_days: int = 30,
) -> dict:
    """
    Upcoming bookings that would fall outside a proposed operating window.

    Derives the slot grid the new window *would* produce for each day in the
    horizon, then reports any live booking whose start is not on that grid.
    """
    now = _aware(now or timezone.now())
    upcoming = list(upcoming_bookings_for_machine(machine, now=now))
    if not upcoming:
        return _impact([])

    # Probe with a detached copy so the real machine is never mutated here.
    probe = Machine(
        hostel_id=machine.hostel_id,
        kind=machine.kind,
        operating_window_start=new_start,
        operating_window_end=new_end,
        slot_length_minutes=machine.slot_length_minutes,
    )

    today = timezone.localdate(now)
    allowed: set[datetime] = set()
    for offset in range(horizon_days + 2):
        for start, _end in iter_operating_slots(probe, today + timedelta(days=offset)):
            allowed.add(start)

    return _impact([b for b in upcoming if _aware(b.starts_at) not in allowed])


def dashboard_summary(*, institute_id, on_date: date_cls, hostel_id=None) -> dict:
    """Headline numbers for one day."""
    machines = Machine.objects.filter(is_active=True)
    students = Student.objects.filter(is_active=True)
    tickets = Ticket.objects.filter(is_active=True, status=TicketStatus.OPEN)
    bookings = Booking.objects.filter(is_active=True, cancelled_at__isnull=True)

    if institute_id is not None:
        machines = machines.filter(hostel__institute_id=institute_id)
        students = students.filter(institute_id=institute_id)
        tickets = tickets.filter(machine__hostel__institute_id=institute_id)
        bookings = bookings.filter(machine__hostel__institute_id=institute_id)
    if hostel_id and str(hostel_id).lower() != "all":
        machines = machines.filter(hostel_id=hostel_id)
        students = students.filter(home_hostel_id=hostel_id)
        tickets = tickets.filter(machine__hostel_id=hostel_id)
        bookings = bookings.filter(machine__hostel_id=hostel_id)

    machine_list = list(machines.select_related("hostel"))
    day_start, day_end = _day_bounds(on_date)
    booked = bookings.filter(starts_at__gte=day_start, starts_at__lt=day_end).count()

    # Capacity is the number of slots the online machines actually offer that
    # day — an offline machine offers none, so it must not inflate the divisor.
    capacity = sum(
        len(list(iter_operating_slots(m, on_date)))
        for m in machine_list
        if not m.is_offline
    )

    now = timezone.now()
    return {
        "date": on_date,
        "bookings": booked,
        "capacity_slots": capacity,
        "capacity_used_pct": round((booked / capacity) * 100) if capacity else 0,
        "open_tickets": tickets.count(),
        "machines_total": len(machine_list),
        "machines_offline": sum(1 for m in machine_list if m.is_offline),
        "students_total": students.count(),
        "suspended_students": students.filter(suspension_ends__gt=now).count(),
    }


def needs_attention(*, institute_id, hostel_id=None, now=None) -> list[dict]:
    """Things an administrator should look at, worst first."""
    now = _aware(now or timezone.now())
    machines = Machine.objects.filter(is_active=True).select_related("hostel")
    tickets = Ticket.objects.filter(is_active=True, status=TicketStatus.OPEN)
    if institute_id is not None:
        machines = machines.filter(hostel__institute_id=institute_id)
        tickets = tickets.filter(machine__hostel__institute_id=institute_id)
    if hostel_id and str(hostel_id).lower() != "all":
        machines = machines.filter(hostel_id=hostel_id)
        tickets = tickets.filter(machine__hostel_id=hostel_id)

    items: list[dict] = []

    maintenance = (
        tickets.filter(kind=TicketKind.MAINTENANCE)
        .values("machine_id")
        .annotate(n=Count("id"))
    )
    by_machine = {row["machine_id"]: row["n"] for row in maintenance}
    machine_by_id = {m.id: m for m in machines}

    for machine_id, count in by_machine.items():
        machine = machine_by_id.get(machine_id)
        if machine is None:
            continue
        items.append(
            {
                "kind": "maintenance_reports",
                "severity": "high" if count > 1 else "medium",
                "title": f"{machine.location_name} has {count} open maintenance report(s)",
                "detail": f"{machine.hostel.name} · {machine.get_kind_display()}",
                "target_type": "machine",
                "target_id": machine.id,
            }
        )

    stale_before = now - timedelta(days=STALE_OFFLINE_DAYS)
    for machine in machines:
        if not machine.is_offline:
            continue
        long_offline = machine.updated_at <= stale_before
        items.append(
            {
                "kind": "machine_offline",
                "severity": "high" if long_offline else "medium",
                "title": (
                    f"{machine.location_name} has been offline since "
                    f"{timezone.localtime(machine.updated_at):%d %b}"
                    if long_offline
                    else f"{machine.location_name} is offline"
                ),
                "detail": f"{machine.hostel.name} · {machine.get_kind_display()}",
                "target_type": "machine",
                "target_id": machine.id,
            }
        )

    rank = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda i: rank.get(i["severity"], 3))
    return items


def recent_activity(*, institute_id, hostel_id=None, limit: int = 30) -> list[dict]:
    """
    A merged feed of bookings, cancellations and administrator actions.

    Three sources, one timeline, newest first — the portal asks for "recent
    activity across the institute", not three separate lists to reconcile.
    """
    from laundry.models import AdminAuditLog

    bookings = Booking.objects.filter(is_active=True).select_related(
        "student", "machine", "machine__hostel"
    )
    audit = AdminAuditLog.objects.select_related("actor")
    if institute_id is not None:
        bookings = bookings.filter(machine__hostel__institute_id=institute_id)
        audit = audit.filter(Q(institute_id=institute_id) | Q(institute__isnull=True))
    if hostel_id and str(hostel_id).lower() != "all":
        bookings = bookings.filter(machine__hostel_id=hostel_id)

    items: list[dict] = []
    for b in bookings.order_by("-created_at")[:limit]:
        items.append(
            {
                "at": b.created_at,
                "kind": "booking",
                "summary": (
                    f"{b.student.name} booked {b.machine.location_name} "
                    f"({b.machine.hostel.name}) for "
                    f"{timezone.localtime(b.starts_at):%d %b %H:%M}"
                ),
                "actor": b.student.name,
                "target_type": "booking",
                "target_id": b.id,
            }
        )
        if b.cancelled_at is not None:
            items.append(
                {
                    "at": b.cancelled_at,
                    "kind": "cancellation",
                    "summary": (
                        f"{b.student.name} cancelled {b.machine.location_name} for "
                        f"{timezone.localtime(b.starts_at):%d %b %H:%M}"
                        + (" (late)" if b.is_late_cancel else "")
                    ),
                    "actor": b.student.name,
                    "target_type": "booking",
                    "target_id": b.id,
                }
            )

    for entry in audit.order_by("-created_at")[:limit]:
        items.append(
            {
                "at": entry.created_at,
                "kind": "admin_action",
                "summary": entry.summary,
                "actor": entry.actor_label,
                "target_type": entry.target_type,
                "target_id": entry.target_id,
            }
        )

    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:limit]
