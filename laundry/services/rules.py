"""
Institute fairness rules: quota, advance window, past slots,
late vs free cancel.

When a rule blocks an action, callers receive which rule and ``clears_at``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from base.exceptions import OUTSIDE_ADVANCE_WINDOW, PAST_SLOT, RULE_BLOCKED
from laundry.models import (
    Booking,
    Institute,
    InstituteRule,
    Machine,
    MachineKind,
    Student,
)

RULE_QUOTA = "quota"
RULE_COOLDOWN = "cooldown"
RULE_ADVANCE_WINDOW = "advance_window"
RULE_PAST_SLOT = "past_slot"


@dataclass(frozen=True)
class RuleBlock:
    rule: str
    clears_at: datetime | None
    detail: str
    code: str = RULE_BLOCKED


def get_institute_rules(institute: Institute) -> InstituteRule:
    try:
        return institute.rules
    except InstituteRule.DoesNotExist:
        return InstituteRule(institute=institute)


def booking_counts_toward_quota(machine: Machine, rules: InstituteRule) -> bool:
    """Whether this booking should count against its machine-kind weekly cap.

    Washers always count against the washer quota. Dryers count against the
    separate dryer cap only when ``dryer_cap_enabled`` — they never consume
    washer quota.
    """
    if machine.kind == MachineKind.WASHER:
        return True
    return bool(rules.dryer_cap_enabled)


def _washer_quota_kinds() -> list[str]:
    return [MachineKind.WASHER]


def _dryer_quota_kinds() -> list[str]:
    return [MachineKind.DRYER]


def _aware(dt: datetime) -> datetime:
    if timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt, timezone.get_current_timezone())


def _quota_counting_starts(
    student: Student,
    kinds: list[str],
    *,
    exclude_booking_id=None,
) -> list[datetime]:
    qs = Booking.objects.filter(
        student=student,
        is_active=True,
        counts_against_quota=True,
        machine__kind__in=kinds,
    )
    if exclude_booking_id:
        qs = qs.exclude(pk=exclude_booking_id)
    return list(qs.values_list("starts_at", flat=True))


def quota_week_bounds(instant: datetime) -> tuple[datetime, datetime]:
    """Monday 00:00 inclusive through next Monday 00:00 exclusive.

    Weeks follow the institute wall clock (``TIME_ZONE``), not UTC.
    """
    local = timezone.localtime(_aware(instant))
    monday = local.date() - timedelta(days=local.weekday())
    start = timezone.make_aware(
        datetime.combine(monday, datetime.min.time()),
        timezone.get_current_timezone(),
    )
    return start, start + timedelta(days=7)


def _starts_in_week(
    starts: list[datetime],
    week_start: datetime,
    week_end: datetime,
) -> list[datetime]:
    return [start for start in starts if week_start <= _aware(start) < week_end]


def check_booking_rules(
    student: Student,
    machine: Machine,
    starts_at: datetime,
    ends_at: datetime,
    *,
    now: datetime | None = None,
    rules: InstituteRule | None = None,
    exclude_booking_id=None,
) -> RuleBlock | None:
    """
    Return a ``RuleBlock`` if the student may not claim this slot, else None.

    Does not check machine offline, gender, or first-come uniqueness.
    """
    now = _aware(now or timezone.now())
    starts_at = _aware(starts_at)
    ends_at = _aware(ends_at)
    rules = rules or get_institute_rules(student.institute)

    if starts_at <= now:
        return RuleBlock(
            rule=RULE_PAST_SLOT,
            clears_at=None,
            detail="That slot has already started.",
            code=PAST_SLOT,
        )

    advance_days = int(rules.advance_window_days or 0)
    local_start = timezone.localtime(starts_at)
    local_now = timezone.localtime(now)
    latest_date = local_now.date() + timedelta(days=advance_days)
    if local_start.date() > latest_date:
        window_opens = timezone.make_aware(
            datetime.combine(
                local_start.date() - timedelta(days=advance_days),
                datetime.min.time(),
            ),
            timezone.get_current_timezone(),
        )
        return RuleBlock(
            rule=RULE_ADVANCE_WINDOW,
            clears_at=window_opens,
            detail=(
                f"You can only book {advance_days} day"
                f"{'s' if advance_days != 1 else ''} ahead."
            ),
            code=OUTSIDE_ADVANCE_WINDOW,
        )

    kinds = _washer_quota_kinds()
    if machine.kind == MachineKind.DRYER:
        if not rules.dryer_cap_enabled:
            return None
        kinds = _dryer_quota_kinds()

    limit = int(rules.quota_limit or 0)
    week_start, week_end = quota_week_bounds(starts_at)
    existing_starts = _quota_counting_starts(
        student,
        kinds,
        exclude_booking_id=exclude_booking_id,
    )
    used = len(_starts_in_week(existing_starts, week_start, week_end))
    if used >= limit:
        if machine.kind == MachineKind.DRYER:
            detail = (
                f"Weekly dryer quota is {limit} booking"
                f"{'s' if limit != 1 else ''} Monday to Sunday."
            )
        else:
            detail = (
                f"Weekly quota is {limit} wash"
                f"{'es' if limit != 1 else ''} Monday to Sunday."
            )
        return RuleBlock(
            rule=RULE_QUOTA,
            clears_at=week_end,
            detail=detail,
            code=RULE_BLOCKED,
        )

    return None


def is_late_cancel(
    booking: Booking,
    *,
    now: datetime | None = None,
    rules: InstituteRule | None = None,
) -> bool:
    now = _aware(now or timezone.now())
    rules = rules or get_institute_rules(booking.student.institute)
    cutoff = timedelta(hours=int(rules.cancellation_cutoff_hours or 0))
    return booking.starts_at - now < cutoff


def quota_status(
    student: Student,
    *,
    now: datetime | None = None,
    rules: InstituteRule | None = None,
) -> dict:
    """
    Monday–Sunday quota usage for profile display.

    Counts washer bookings that ``counts_against_quota`` (same as
    ``check_booking_rules``). Bookings in other weeks are excluded; upcoming
    starts later this week still consume this week's quota. ``resets_at`` is
    always next Monday 00:00.

    ``dryer_used`` / ``dryer_limit`` are a separate dryer weekly cap when
    ``dryer_cap_enabled`` (limit matches ``quota_limit``). Dryers never share
    the washer quota.
    """
    now = _aware(now or timezone.now())
    rules = rules or get_institute_rules(student.institute)
    window_days = int(rules.quota_window_days or 7)
    limit = int(rules.quota_limit or 0)
    week_start, week_end = quota_week_bounds(now)
    starts = _quota_counting_starts(student, _washer_quota_kinds())
    used = len(_starts_in_week(starts, week_start, week_end))
    dryer_starts = list(
        Booking.objects.filter(
            student=student,
            is_active=True,
            cancelled_at__isnull=True,
            machine__kind=MachineKind.DRYER,
            starts_at__gte=week_start,
            starts_at__lt=week_end,
        ).values_list("starts_at", flat=True)
    )
    dryer_limit = limit if rules.dryer_cap_enabled else 0
    return {
        "used": used,
        "limit": limit,
        "dryer_used": len(dryer_starts),
        "dryer_limit": dryer_limit,
        "window_days": window_days,
        "resets_at": week_end,
    }


def cooldown_clears_at(
    student: Student,
    *,
    now: datetime | None = None,
    rules: InstituteRule | None = None,
) -> datetime | None:
    """Cooldown is not enforced. Always ``None``."""
    return None


def student_gender(student: Student) -> str:
    """Admin-assigned demographic gender, or blank until set."""
    return student.gender or ""


def visible_hostels(student: Student):
    """Active hostels in the student's institute (any hostel is eligible)."""
    return student.institute.hostels.filter(is_active=True)


def machine_is_visible(student: Student, machine: Machine) -> bool:
    """Whether a student may list slots or book on this machine.

    Eligibility is institute-scoped — not limited to ``home_hostel``.
    Students may book in any active hostel returned by ``visible_hostels``.
    """
    if not machine.is_active or not machine.hostel.is_active:
        return False
    if machine.hostel.institute_id != student.institute_id:
        return False
    return True
