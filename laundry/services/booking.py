"""
Create, cancel, and move bookings.

Slot claims are first-come and must never double-book a machine.

Every write that claims an interval — create and move alike — runs inside one
transaction that locks the machine row, re-checks for an *overlapping* live
booking, and only then writes. Checking outside the transaction, or comparing
only ``starts_at`` for equality, both let two students hold the same machine at
once; see ``overlapping_bookings`` for why equality is not enough. The partial
unique index on (machine, starts_at) remains as a last-resort backstop.

Washer + dryer together are two independent bookings with per-item results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status

from base.exceptions import (
    APIError,
    MACHINE_OFFLINE,
    PAST_SLOT,
    RULE_BLOCKED,
    SLOT_TAKEN,
)
from laundry.models import (
    AvailabilityMiss,
    Booking,
    BookingChannel,
    Machine,
    NotificationKind,
    NotificationType,
    Student,
)
from laundry.services.access import assert_can_mutate
from laundry.services.notifications import create_in_app_notification
from laundry.services.rules import (
    RuleBlock,
    booking_counts_toward_quota,
    check_booking_rules,
    get_institute_rules,
    is_late_cancel,
    machine_is_visible,
    visible_hostels,
)
from laundry.services.slots import (
    SLOT_FREE,
    derive_slots,
    load_overlapping_bookings,
    resolve_slot,
    slot_from_date_hour,
)

MOVE_OPTIONS_LIMIT = 40


@dataclass
class BookingRequest:
    machine_id: UUID | str
    starts_at: datetime | None = None
    date: date | None = None
    hour: int | None = None


@dataclass
class BookingItemResult:
    ok: bool
    booking: Booking | None = None
    code: str | None = None
    detail: str | None = None
    rule: str | None = None
    clears_at: datetime | None = None
    machine_id: str | None = None


def _aware(dt: datetime) -> datetime:
    if timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt, timezone.get_current_timezone())


def _block_result(block: RuleBlock, machine_id=None) -> BookingItemResult:
    return BookingItemResult(
        ok=False,
        code=block.code,
        detail=block.detail,
        rule=block.rule,
        clears_at=block.clears_at,
        machine_id=str(machine_id) if machine_id else None,
    )


def raise_rule_block(block: RuleBlock) -> None:
    extra = {"rule": block.rule, "clearsAt": None}
    if block.clears_at is not None:
        extra["clearsAt"] = block.clears_at.isoformat()
    raise APIError(block.code, detail=block.detail, extra=extra)


def _resolve_requested_slot(
    machine: Machine,
    req: BookingRequest,
) -> tuple[datetime, datetime] | None:
    if req.starts_at is not None:
        return resolve_slot(machine, _aware(req.starts_at))
    if req.date is not None and req.hour is not None:
        return slot_from_date_hour(machine, req.date, int(req.hour))
    return None


def _load_visible_machine(student: Student, machine_id) -> Machine | None:
    try:
        machine = Machine.objects.select_related("hostel", "hostel__institute").get(
            pk=machine_id,
            is_active=True,
        )
    except (Machine.DoesNotExist, ValueError, TypeError):
        return None
    if not machine_is_visible(student, machine):
        return None
    return machine


def overlapping_bookings(
    machine: Machine,
    starts_at: datetime,
    ends_at: datetime,
    *,
    exclude_booking_id=None,
):
    """
    Active bookings on ``machine`` whose interval intersects [starts_at, ends_at).

    Half-open on purpose: a booking that ends exactly when another starts does
    not overlap, which is what back-to-back washer→dryer runs depend on.

    Checking the *interval* rather than an exact ``starts_at`` match matters
    because the slot grid is derived, not stored. Narrowing a machine's slot
    length or shifting its operating window re-cuts the grid under bookings that
    already exist, so a newly derived slot can straddle an old booking while
    starting at a different minute. An equality check waves those through.
    """
    qs = Booking.objects.filter(
        machine=machine,
        is_active=True,
        cancelled_at__isnull=True,
        starts_at__lt=ends_at,
        ends_at__gt=starts_at,
    )
    if exclude_booking_id:
        qs = qs.exclude(pk=exclude_booking_id)
    return qs


def _lock_machine(machine: Machine) -> None:
    """
    Serialize every claim on one machine for the rest of the transaction.

    Two students racing for the same slot queue behind this lock, so the overlap
    check that follows reads a committed view instead of a stale one. On
    PostgreSQL this is a row lock. SQLite has no SELECT … FOR UPDATE — Django
    skips the clause there — but SQLite serializes writers at the database level
    and the partial unique index still backstops identical start times.
    """
    Machine.objects.select_for_update().filter(pk=machine.pk).first()


def _notify_booking_confirmed(booking: Booking) -> None:
    when = timezone.localtime(booking.starts_at).strftime("%Y-%m-%d %H:%M")
    create_in_app_notification(
        student=booking.student,
        title="Booking confirmed",
        body=f"{booking.machine.location_name} · {when} is yours.",
        notification_type=NotificationType.BOOKING_CONFIRMED,
        kind=NotificationKind.SUCCESS,
        related_object_type="booking",
        related_object_id=booking.id,
        preference_field="booking_confirmed",
    )


def _create_one(
    student: Student,
    req: BookingRequest,
    *,
    now: datetime,
    exclude_booking_id=None,
    notify: bool = True,
    channel: str = BookingChannel.APP,
) -> BookingItemResult:
    machine = _load_visible_machine(student, req.machine_id)
    if machine is None:
        return BookingItemResult(
            ok=False,
            code="NOT_FOUND",
            detail="Machine not found.",
            machine_id=str(req.machine_id) if req.machine_id else None,
        )
    if machine.is_offline:
        return BookingItemResult(
            ok=False,
            code=MACHINE_OFFLINE,
            detail="That machine is out of service.",
            machine_id=str(machine.id),
        )

    resolved = _resolve_requested_slot(machine, req)
    if resolved is None:
        return BookingItemResult(
            ok=False,
            code="VALIDATION_ERROR",
            detail="That time is not a valid slot for this machine.",
            machine_id=str(machine.id),
        )
    starts_at, ends_at = resolved
    rules = get_institute_rules(student.institute)

    block = check_booking_rules(
        student,
        machine,
        starts_at,
        ends_at,
        now=now,
        rules=rules,
        exclude_booking_id=exclude_booking_id,
    )
    if block is not None:
        return _block_result(block, machine.id)

    counts = booking_counts_toward_quota(machine, rules)
    taken_result = BookingItemResult(
        ok=False,
        code=SLOT_TAKEN,
        detail="That slot was just taken.",
        machine_id=str(machine.id),
    )

    # The availability check and the insert have to be one indivisible step.
    # Checked outside the transaction, two concurrent requests both read "free"
    # and both write — the classic check-then-act race, and the reason this slot
    # could be double-booked.
    try:
        with transaction.atomic():
            _lock_machine(machine)
            if overlapping_bookings(
                machine,
                starts_at,
                ends_at,
                exclude_booking_id=exclude_booking_id,
            ).exists():
                return taken_result

            booking = Booking.objects.create(
                student=student,
                machine=machine,
                starts_at=starts_at,
                ends_at=ends_at,
                counts_against_quota=counts,
                is_late_cancel=False,
                channel=channel,
            )
    except IntegrityError:
        # Last line of defence: the partial unique index on
        # (machine, starts_at) for live bookings. Reachable only if a writer
        # slipped past the lock, which the lock is there to prevent.
        return taken_result

    booking = Booking.objects.select_related(
        "machine",
        "machine__hostel",
        "student",
    ).get(pk=booking.pk)
    if notify:
        _notify_booking_confirmed(booking)
    return BookingItemResult(ok=True, booking=booking, machine_id=str(machine.id))


def create_bookings(
    student: Student,
    items: list[BookingRequest],
    *,
    now: datetime | None = None,
    channel: str = BookingChannel.APP,
) -> list[BookingItemResult]:
    """
    Attempt each item independently (washer + dryer partial success).

    Raises APIError for UNVERIFIED / SUSPENDED (whole request).

    ``channel`` records where the booking came from. Callers resolve it with
    ``base.clients.resolve_channel(request)`` rather than reading it from the
    request body — see ``base/clients.py`` for why.
    """
    now = _aware(now or timezone.now())
    assert_can_mutate(student, now=now)
    results: list[BookingItemResult] = []
    for req in items:
        results.append(_create_one(student, req, now=now, channel=channel))
    return results


def get_own_booking(student: Student, booking_id) -> Booking:
    try:
        return Booking.objects.select_related(
            "machine",
            "machine__hostel",
            "student",
            "student__institute",
        ).get(pk=booking_id, student=student, is_active=True)
    except (Booking.DoesNotExist, ValueError, TypeError) as exc:
        raise APIError(
            "NOT_FOUND",
            detail="Booking not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        ) from exc


def cancel_booking(
    student: Student,
    booking: Booking,
    *,
    now: datetime | None = None,
) -> Booking:
    now = _aware(now or timezone.now())
    assert_can_mutate(student, now=now)
    if booking.student_id != student.pk:
        raise APIError(
            "NOT_FOUND",
            detail="Booking not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if booking.cancelled_at is not None:
        return booking
    if booking.starts_at <= now:
        raise APIError(PAST_SLOT, detail="You can't cancel a booking that has started.")

    rules = get_institute_rules(student.institute)
    late = is_late_cancel(booking, now=now, rules=rules)
    booking.cancelled_at = now
    booking.is_late_cancel = late
    if not late:
        booking.counts_against_quota = False
    booking.save(
        update_fields=[
            "cancelled_at",
            "is_late_cancel",
            "counts_against_quota",
            "updated_at",
        ]
    )
    return booking


def move_booking(
    student: Student,
    booking: Booking,
    req: BookingRequest,
    *,
    now: datetime | None = None,
) -> Booking:
    """Move to a new slot; old slot is released on success. Rules re-checked."""
    now = _aware(now or timezone.now())
    assert_can_mutate(student, now=now)
    if booking.student_id != student.pk:
        raise APIError(
            "NOT_FOUND",
            detail="Booking not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if booking.cancelled_at is not None:
        raise APIError(
            PAST_SLOT,
            detail="That booking is no longer active.",
        )
    if booking.starts_at <= now:
        raise APIError(PAST_SLOT, detail="You can't move a booking that has started.")

    machine = _load_visible_machine(student, req.machine_id or booking.machine_id)
    if machine is None:
        raise APIError(
            "NOT_FOUND",
            detail="Machine not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if machine.kind != booking.machine.kind:
        raise APIError(
            "VALIDATION_ERROR",
            detail="Move the booking to another machine of the same kind.",
        )
    if machine.is_offline:
        raise APIError(MACHINE_OFFLINE, detail="That machine is out of service.")

    resolved = _resolve_requested_slot(machine, req)
    if resolved is None:
        raise APIError(
            "VALIDATION_ERROR",
            detail="That time is not a valid slot for this machine.",
        )
    starts_at, ends_at = resolved

    if machine.pk == booking.machine_id and starts_at == booking.starts_at:
        return booking

    rules = get_institute_rules(student.institute)
    block = check_booking_rules(
        student,
        machine,
        starts_at,
        ends_at,
        now=now,
        rules=rules,
        exclude_booking_id=booking.pk,
    )
    if block is not None:
        raise_rule_block(block)

    booking.machine = machine
    booking.starts_at = starts_at
    booking.ends_at = ends_at
    booking.counts_against_quota = booking_counts_toward_quota(machine, rules)
    booking.is_late_cancel = False
    try:
        # Same indivisible check-and-write as _create_one: a move is a claim on
        # the destination slot and races exactly like a fresh booking does.
        # The booking being moved is excluded so it never collides with itself.
        with transaction.atomic():
            _lock_machine(machine)
            if overlapping_bookings(
                machine, starts_at, ends_at, exclude_booking_id=booking.pk
            ).exists():
                raise APIError(
                    SLOT_TAKEN,
                    detail="That slot was just taken.",
                    status_code=status.HTTP_409_CONFLICT,
                )
            booking.save(
                update_fields=[
                    "machine",
                    "starts_at",
                    "ends_at",
                    "counts_against_quota",
                    "is_late_cancel",
                    "updated_at",
                ]
            )
    except IntegrityError as exc:
        raise APIError(
            SLOT_TAKEN,
            detail="That slot was just taken.",
            status_code=status.HTTP_409_CONFLICT,
        ) from exc

    return Booking.objects.select_related("machine", "machine__hostel", "student").get(
        pk=booking.pk
    )


def move_options(
    student: Student,
    booking: Booking,
    *,
    now: datetime | None = None,
    limit: int = MOVE_OPTIONS_LIMIT,
) -> list[dict]:
    """Upcoming free slots of the same kind the student could move this booking to."""
    now = _aware(now or timezone.now())
    if booking.cancelled_at is not None or booking.starts_at <= now:
        return []

    rules = get_institute_rules(student.institute)
    hostels = list(visible_hostels(student))
    machines = list(
        Machine.objects.filter(
            hostel__in=hostels,
            kind=booking.machine.kind,
            is_active=True,
            is_offline=False,
        ).select_related("hostel")
    )
    if not machines:
        return []

    advance_days = int(rules.advance_window_days or 0)
    today = timezone.localtime(now).date()
    dates = [today + timedelta(days=offset) for offset in range(advance_days + 1)]
    range_start = timezone.make_aware(
        datetime.combine(today, datetime.min.time()),
        timezone.get_current_timezone(),
    )
    range_end = timezone.make_aware(
        datetime.combine(dates[-1] + timedelta(days=1), datetime.min.time()),
        timezone.get_current_timezone(),
    )
    bookings = load_overlapping_bookings(machines, range_start, range_end)

    options: list[dict] = []
    for on_date in dates:
        if len(options) >= limit:
            break
        for machine in machines:
            if len(options) >= limit:
                break
            for slot in derive_slots(
                machine,
                on_date,
                student=student,
                now=now,
                bookings=bookings,
                rules=rules,
            ):
                if slot.booking_id == booking.pk:
                    continue
                if slot.state != SLOT_FREE:
                    continue
                if machine.pk == booking.machine_id and slot.starts_at == booking.starts_at:
                    continue
                block = check_booking_rules(
                    student,
                    machine,
                    slot.starts_at,
                    slot.ends_at,
                    now=now,
                    rules=rules,
                    exclude_booking_id=booking.pk,
                )
                if block is not None:
                    continue
                options.append(
                    {
                        "machine": machine,
                        "starts_at": slot.starts_at,
                        "ends_at": slot.ends_at,
                        "hour": slot.hour,
                    }
                )
                if len(options) >= limit:
                    break
    options.sort(key=lambda row: (row["starts_at"], row["machine"].location_name))
    return options[:limit]


def record_availability_miss(
    student: Student,
    machine: Machine,
    on_date: date,
    hour: int,
    *,
    now: datetime | None = None,
) -> AvailabilityMiss:
    now = _aware(now or timezone.now())
    assert_can_mutate(student, now=now)
    if not machine_is_visible(student, machine):
        raise APIError(
            "NOT_FOUND",
            detail="Machine not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    miss, _created = AvailabilityMiss.objects.get_or_create(
        student=student,
        machine=machine,
        date=on_date,
        hour=int(hour),
    )
    return miss


def upcoming_bookings_qs(student: Student, *, now: datetime | None = None):
    now = _aware(now or timezone.now())
    return (
        Booking.objects.filter(
            student=student,
            is_active=True,
            cancelled_at__isnull=True,
            ends_at__gt=now,
        )
        .select_related("machine", "machine__hostel")
        .order_by("starts_at")
    )


def past_bookings_qs(student: Student, *, now: datetime | None = None):
    now = _aware(now or timezone.now())
    return (
        Booking.objects.filter(student=student, is_active=True)
        .filter(Q(ends_at__lte=now) | Q(cancelled_at__isnull=False))
        .select_related("machine", "machine__hostel")
        .order_by("-starts_at")
    )
