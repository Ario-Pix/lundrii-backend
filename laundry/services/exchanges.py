"""
Exchange requests and swaps.

Rules are checked at approval, not when the request is sent. Pending rows
expire lazily if a slot is cancelled or the earlier slot has started.
"""

from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status

from base.exceptions import APIError, NOT_FOUND, VALIDATION_ERROR
from laundry.models import (
    Booking,
    Exchange,
    ExchangeKind,
    ExchangeStatus,
    NotificationKind,
    NotificationType,
    Student,
)
from laundry.services.booking import assert_can_mutate
from laundry.services.notifications import create_in_app_notification
from laundry.services.rules import (
    booking_counts_toward_quota,
    check_booking_rules,
    get_institute_rules,
    machine_is_visible,
)

EXCHANGE_PREFETCH = (
    "requester",
    "holder",
    "target_booking",
    "target_booking__machine",
    "target_booking__machine__hostel",
    "target_booking__student",
    "offered_booking",
    "offered_booking__machine",
    "offered_booking__machine__hostel",
    "offered_booking__student",
)


def _aware(dt: datetime) -> datetime:
    if timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt, timezone.get_current_timezone())


def _slot_label(booking: Booking) -> str:
    when = timezone.localtime(booking.starts_at).strftime("%Y-%m-%d %H:%M")
    return f"{booking.machine.location_name} · {when}"


def _format_block_reason(student: Student, detail: str, clears_at=None) -> str:
    reason = f"{student.name} can no longer take this slot: {detail}"
    if clears_at is not None:
        reason = f"{reason} Clears at {clears_at.isoformat()}."
    return reason


def _eligibility_reason(
    student: Student,
    booking: Booking,
    *,
    now: datetime,
    exclude_booking_id=None,
) -> str | None:
    if not student.is_email_verified:
        return _format_block_reason(student, "Email is not verified.")
    if student.is_suspended:
        ends = student.suspension_ends.isoformat() if student.suspension_ends else "later"
        return _format_block_reason(student, f"Account is suspended until {ends}.")
    machine = booking.machine
    if not machine_is_visible(student, machine):
        return _format_block_reason(student, "That machine is not available to this student.")
    if machine.is_offline:
        return _format_block_reason(student, "That machine is out of service.")
    rules = get_institute_rules(student.institute)
    block = check_booking_rules(
        student,
        machine,
        booking.starts_at,
        booking.ends_at,
        now=now,
        rules=rules,
        exclude_booking_id=exclude_booking_id,
    )
    if block is not None:
        return _format_block_reason(student, block.detail, block.clears_at)
    return None


def _is_stale(exchange: Exchange, now: datetime) -> bool:
    target = exchange.target_booking
    if target is None or not target.is_active or target.cancelled_at is not None:
        return True
    if target.starts_at <= now:
        return True
    if target.student_id != exchange.holder_id:
        return True
    offered = exchange.offered_booking
    if offered is not None:
        if not offered.is_active or offered.cancelled_at is not None:
            return True
        if offered.starts_at <= now:
            return True
        if offered.student_id != exchange.requester_id:
            return True
    return False


def _notify_request(exchange: Exchange) -> None:
    kind_label = "swap offer" if exchange.kind == ExchangeKind.SWAP else "request"
    create_in_app_notification(
        student=exchange.holder,
        title="Exchange request",
        body=f"{exchange.requester.name} sent a {kind_label} for {_slot_label(exchange.target_booking)}.",
        notification_type=NotificationType.EXCHANGE_REQUEST,
        kind=NotificationKind.INFO,
        related_object_type="exchange",
        related_object_id=exchange.id,
        preference_field="exchange_request",
    )


def _notify_outcome(student: Student, exchange: Exchange, title: str, body: str) -> None:
    create_in_app_notification(
        student=student,
        title=title,
        body=body,
        notification_type=NotificationType.EXCHANGE_OUTCOME,
        kind=NotificationKind.WARN
        if exchange.status in (ExchangeStatus.FAILED, ExchangeStatus.EXPIRED, ExchangeStatus.REJECTED)
        else NotificationKind.SUCCESS,
        related_object_type="exchange",
        related_object_id=exchange.id,
        preference_field="exchange_outcome",
    )


def _notify_both_outcome(exchange: Exchange, title: str, body: str) -> None:
    _notify_outcome(exchange.requester, exchange, title, body)
    if exchange.holder_id != exchange.requester_id:
        _notify_outcome(exchange.holder, exchange, title, body)


def _mark_expired(exchange: Exchange, now: datetime, *, notify: bool = True) -> Exchange:
    exchange.status = ExchangeStatus.EXPIRED
    exchange.resolved_at = now
    exchange.save(update_fields=["status", "resolved_at", "updated_at"])
    if notify:
        _notify_both_outcome(
            exchange,
            "Exchange expired",
            "This exchange expired because a slot was cancelled or has started.",
        )
    return exchange


def expire_stale_pendings(*, student: Student | None = None, now: datetime | None = None) -> int:
    """Expire pending exchanges whose slots are cancelled or have started."""
    now = _aware(now or timezone.now())
    qs = Exchange.objects.filter(status=ExchangeStatus.PENDING).select_related(*EXCHANGE_PREFETCH)
    if student is not None:
        qs = qs.filter(Q(requester=student) | Q(holder=student))
    expired = 0
    for exchange in qs:
        if _is_stale(exchange, now):
            _mark_expired(exchange, now)
            expired += 1
    return expired


def exchanges_qs(student: Student, *, direction: str | None = None):
    expire_stale_pendings(student=student)
    qs = Exchange.objects.filter(Q(requester=student) | Q(holder=student)).select_related(
        *EXCHANGE_PREFETCH
    )
    if direction == "incoming":
        return qs.filter(holder=student)
    if direction == "outgoing":
        return qs.filter(requester=student)
    return qs


def get_exchange_for_student(student: Student, exchange_id, *, now: datetime | None = None) -> Exchange:
    now = _aware(now or timezone.now())
    expire_stale_pendings(student=student, now=now)
    try:
        exchange = Exchange.objects.select_related(*EXCHANGE_PREFETCH).get(pk=exchange_id)
    except (Exchange.DoesNotExist, ValueError, TypeError) as exc:
        raise APIError(
            NOT_FOUND,
            detail="Exchange not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        ) from exc
    if exchange.requester_id != student.pk and exchange.holder_id != student.pk:
        raise APIError(
            NOT_FOUND,
            detail="Exchange not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return exchange


def _load_booking(booking_id) -> Booking | None:
    try:
        return Booking.objects.select_related(
            "machine",
            "machine__hostel",
            "student",
            "student__institute",
            "student__user",
        ).get(pk=booking_id, is_active=True)
    except (Booking.DoesNotExist, ValueError, TypeError):
        return None


def create_exchange(
    requester: Student,
    *,
    kind: str,
    target_booking_id,
    offered_booking_id=None,
    now: datetime | None = None,
) -> Exchange:
    now = _aware(now or timezone.now())
    assert_can_mutate(requester, now=now)
    expire_stale_pendings(student=requester, now=now)

    kind = (kind or "").lower()
    if kind not in (ExchangeKind.REQUEST, ExchangeKind.SWAP):
        raise APIError(VALIDATION_ERROR, detail="kind must be request or swap.")

    target = _load_booking(target_booking_id)
    if target is None:
        raise APIError(
            NOT_FOUND,
            detail="Target booking not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if target.student_id == requester.pk:
        raise APIError(VALIDATION_ERROR, detail="You already hold that slot.")
    if target.cancelled_at is not None:
        raise APIError(VALIDATION_ERROR, detail="That booking is no longer active.")
    if target.starts_at <= now:
        raise APIError(VALIDATION_ERROR, detail="That slot has already started.")
    if target.student.institute_id != requester.institute_id:
        raise APIError(
            NOT_FOUND,
            detail="Target booking not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if not machine_is_visible(requester, target.machine):
        raise APIError(
            NOT_FOUND,
            detail="Target booking not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    offered = None
    if kind == ExchangeKind.SWAP:
        if offered_booking_id is None:
            raise APIError(VALIDATION_ERROR, detail="Swap requires offeredBookingId.")
        offered = _load_booking(offered_booking_id)
        if offered is None or offered.student_id != requester.pk:
            raise APIError(
                NOT_FOUND,
                detail="Offered booking not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if offered.cancelled_at is not None:
            raise APIError(VALIDATION_ERROR, detail="Your offered booking is no longer active.")
        if offered.starts_at <= now:
            raise APIError(VALIDATION_ERROR, detail="Your offered slot has already started.")
        if offered.pk == target.pk:
            raise APIError(VALIDATION_ERROR, detail="Offer a different booking than the one you want.")
        if Exchange.objects.filter(
            status=ExchangeStatus.PENDING,
            offered_booking=offered,
        ).exists():
            raise APIError(
                VALIDATION_ERROR,
                detail="That booking is already offered in another pending exchange.",
            )
    elif offered_booking_id is not None:
        raise APIError(VALIDATION_ERROR, detail="Requests cannot include an offered booking.")

    if Exchange.objects.filter(
        requester=requester,
        target_booking=target,
        status=ExchangeStatus.PENDING,
    ).exists():
        raise APIError(VALIDATION_ERROR, detail="You already have a pending request for that slot.")

    exchange = Exchange.objects.create(
        kind=kind,
        status=ExchangeStatus.PENDING,
        requester=requester,
        holder=target.student,
        target_booking=target,
        offered_booking=offered,
    )
    exchange = Exchange.objects.select_related(*EXCHANGE_PREFETCH).get(pk=exchange.pk)
    _notify_request(exchange)
    return exchange


def _fail_exchange(exchange: Exchange, reason: str, now: datetime) -> Exchange:
    exchange.status = ExchangeStatus.FAILED
    exchange.failure_reason = reason
    exchange.resolved_at = now
    exchange.save(update_fields=["status", "failure_reason", "resolved_at", "updated_at"])
    _notify_both_outcome(exchange, "Exchange failed", reason)
    return Exchange.objects.select_related(*EXCHANGE_PREFETCH).get(pk=exchange.pk)


def _assert_pending(exchange: Exchange) -> None:
    if exchange.status != ExchangeStatus.PENDING:
        raise APIError(
            VALIDATION_ERROR,
            detail=f"Exchange is {exchange.status}, not pending.",
        )


def approve_exchange(
    holder: Student,
    exchange: Exchange,
    *,
    now: datetime | None = None,
) -> Exchange:
    now = _aware(now or timezone.now())
    assert_can_mutate(holder, now=now)
    if exchange.holder_id != holder.pk:
        raise APIError(
            NOT_FOUND,
            detail="Exchange not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    expire_stale_pendings(student=holder, now=now)
    exchange = Exchange.objects.select_related(*EXCHANGE_PREFETCH).get(pk=exchange.pk)
    if exchange.status == ExchangeStatus.EXPIRED:
        return exchange
    _assert_pending(exchange)

    with transaction.atomic():
        locked = (
            Exchange.objects.select_for_update()
            .select_related(*EXCHANGE_PREFETCH)
            .get(pk=exchange.pk)
        )
        if locked.status != ExchangeStatus.PENDING:
            return locked
        if _is_stale(locked, now):
            return _mark_expired(locked, now)

        target = Booking.objects.select_for_update().select_related(
            "machine",
            "machine__hostel",
            "student",
            "student__institute",
        ).get(pk=locked.target_booking_id)
        offered = None
        if locked.offered_booking_id:
            offered = Booking.objects.select_for_update().select_related(
                "machine",
                "machine__hostel",
                "student",
                "student__institute",
            ).get(pk=locked.offered_booking_id)

        requester = locked.requester
        if target.student_id != locked.holder_id:
            return _fail_exchange(
                locked,
                f"{locked.holder.name}'s slot is no longer theirs.",
                now,
            )
        if locked.kind == ExchangeKind.SWAP:
            if offered is None:
                return _fail_exchange(locked, "The offered booking is missing.", now)
            if offered.student_id != requester.pk:
                return _fail_exchange(
                    locked,
                    f"{requester.name}'s offered slot is no longer theirs.",
                    now,
                )

        reasons: list[str] = []
        if locked.kind == ExchangeKind.REQUEST:
            reason = _eligibility_reason(requester, target, now=now)
            if reason:
                reasons.append(reason)
        else:
            reason = _eligibility_reason(
                requester,
                target,
                now=now,
                exclude_booking_id=offered.pk if offered else None,
            )
            if reason:
                reasons.append(reason)
            holder_reason = _eligibility_reason(
                locked.holder,
                offered,
                now=now,
                exclude_booking_id=target.pk,
            )
            if holder_reason:
                reasons.append(holder_reason)

        if reasons:
            return _fail_exchange(locked, " ".join(reasons), now)

        if locked.kind == ExchangeKind.REQUEST:
            target.student = requester
            target.counts_against_quota = booking_counts_toward_quota(
                target.machine,
                get_institute_rules(requester.institute),
            )
            target.save(update_fields=["student", "counts_against_quota", "updated_at"])
        else:
            offered.student, target.student = locked.holder, requester
            offered.save(update_fields=["student", "updated_at"])
            target.save(update_fields=["student", "updated_at"])

        locked.status = ExchangeStatus.APPROVED
        locked.resolved_at = now
        locked.failure_reason = ""
        locked.save(update_fields=["status", "resolved_at", "failure_reason", "updated_at"])

    exchange = Exchange.objects.select_related(*EXCHANGE_PREFETCH).get(pk=locked.pk)
    if exchange.kind == ExchangeKind.REQUEST:
        body = (
            f"{requester.name} now holds {_slot_label(exchange.target_booking)}. "
            "It counts against their quota."
        )
    else:
        body = (
            f"{requester.name} and {exchange.holder.name} swapped slots. "
            "Quota is unchanged."
        )
    _notify_both_outcome(exchange, "Exchange approved", body)
    return exchange


def reject_exchange(
    holder: Student,
    exchange: Exchange,
    *,
    note: str = "",
    now: datetime | None = None,
) -> Exchange:
    now = _aware(now or timezone.now())
    if exchange.holder_id != holder.pk:
        raise APIError(
            NOT_FOUND,
            detail="Exchange not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    expire_stale_pendings(student=holder, now=now)

    with transaction.atomic():
        locked = (
            Exchange.objects.select_for_update()
            .select_related(*EXCHANGE_PREFETCH)
            .get(pk=exchange.pk)
        )
        if locked.holder_id != holder.pk:
            raise APIError(
                NOT_FOUND,
                detail="Exchange not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if locked.status == ExchangeStatus.EXPIRED:
            return locked
        _assert_pending(locked)
        if _is_stale(locked, now):
            return _mark_expired(locked, now)
        locked.status = ExchangeStatus.REJECTED
        locked.resolved_at = now
        locked.reject_note = (note or "").strip()
        locked.save(update_fields=["status", "resolved_at", "reject_note", "updated_at"])

    exchange = Exchange.objects.select_related(*EXCHANGE_PREFETCH).get(pk=locked.pk)
    body = f"{holder.name} declined your exchange request."
    if exchange.reject_note:
        body = f"{body} Note: {exchange.reject_note}"
    _notify_outcome(
        exchange.requester,
        exchange,
        "Exchange declined",
        body,
    )
    return exchange


def withdraw_exchange(
    requester: Student,
    exchange: Exchange,
    *,
    now: datetime | None = None,
) -> Exchange:
    now = _aware(now or timezone.now())
    if exchange.requester_id != requester.pk:
        raise APIError(
            NOT_FOUND,
            detail="Exchange not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    expire_stale_pendings(student=requester, now=now)

    with transaction.atomic():
        locked = (
            Exchange.objects.select_for_update()
            .select_related(*EXCHANGE_PREFETCH)
            .get(pk=exchange.pk)
        )
        if locked.requester_id != requester.pk:
            raise APIError(
                NOT_FOUND,
                detail="Exchange not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if locked.status == ExchangeStatus.EXPIRED:
            return locked
        _assert_pending(locked)
        if _is_stale(locked, now):
            return _mark_expired(locked, now)
        locked.status = ExchangeStatus.REJECTED
        locked.resolved_at = now
        locked.failure_reason = "Withdrawn by requester."
        locked.save(update_fields=["status", "resolved_at", "failure_reason", "updated_at"])

    exchange = Exchange.objects.select_related(*EXCHANGE_PREFETCH).get(pk=locked.pk)
    _notify_outcome(
        exchange.holder,
        exchange,
        "Exchange withdrawn",
        f"{requester.name} withdrew their exchange request.",
    )
    return exchange
