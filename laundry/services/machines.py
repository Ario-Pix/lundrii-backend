"""Machine admin operations (offline toggle + forced booking cancels)."""

from django.db import transaction
from django.utils import timezone

from laundry.models import Booking, Notification, NotificationKind, NotificationType
from laundry.services.notifications import student_allows_notification


def set_machine_offline(machine, is_offline: bool) -> int:
    """
    Set ``machine.is_offline``.

    When transitioning online → offline, cancel future bookings and create
    in-app notifications for affected students. Returns cancelled count.
    """
    was_offline = machine.is_offline
    with transaction.atomic():
        machine.is_offline = is_offline
        machine.save(update_fields=["is_offline", "updated_at"])
        if is_offline and not was_offline:
            return cancel_future_bookings_for_machine(machine)
    return 0


def cancel_future_bookings_for_machine(machine) -> int:
    now = timezone.now()
    bookings = list(
        Booking.objects.select_related("student")
        .filter(machine=machine, cancelled_at__isnull=True, starts_at__gt=now)
        .order_by("starts_at")
    )
    if not bookings:
        return 0

    booking_ids = [b.id for b in bookings]
    Booking.objects.filter(id__in=booking_ids).update(
        cancelled_at=now,
        is_late_cancel=False,
        counts_against_quota=False,
        updated_at=now,
    )

    notifications = []
    for booking in bookings:
        if not student_allows_notification(booking.student, "booking_cancelled_offline"):
            continue
        when = timezone.localtime(booking.starts_at).strftime("%Y-%m-%d %H:%M")
        notifications.append(
            Notification(
                student=booking.student,
                title="Booking cancelled",
                body=(
                    f"Your booking for {machine.location_name} on {when} was "
                    "cancelled because the machine was taken offline."
                ),
                type=NotificationType.BOOKING_CANCELLED_OFFLINE,
                kind=NotificationKind.WARN,
                related_object_type="booking",
                related_object_id=booking.id,
            )
        )
    if notifications:
        Notification.objects.bulk_create(notifications)
    return len(bookings)


def cancel_bookings_outside_hours(machine, booking_ids, *, actor=None) -> int:
    """
    Cancel specific bookings stranded by a narrowed operating window.

    Takes explicit ids rather than re-deriving the set, so what gets cancelled
    is exactly what the administrator was shown in the impact preview. Deriving
    it a second time would let the two answers drift apart between the preview
    and the confirmation.

    Cancellations here are the institute's doing, not the student's, so they
    never count against quota and are never marked a late cancel.
    """
    if not booking_ids:
        return 0

    now = timezone.now()
    with transaction.atomic():
        bookings = list(
            Booking.objects.select_related("student")
            .filter(
                id__in=list(booking_ids),
                machine=machine,
                cancelled_at__isnull=True,
                starts_at__gt=now,
            )
            .order_by("starts_at")
        )
        if not bookings:
            return 0

        Booking.objects.filter(id__in=[b.id for b in bookings]).update(
            cancelled_at=now,
            is_late_cancel=False,
            counts_against_quota=False,
            updated_at=now,
        )

        notifications = []
        for booking in bookings:
            if not student_allows_notification(
                booking.student, "booking_cancelled_offline"
            ):
                continue
            when = timezone.localtime(booking.starts_at).strftime("%Y-%m-%d %H:%M")
            notifications.append(
                Notification(
                    student=booking.student,
                    title="Booking cancelled",
                    body=(
                        f"Your booking for {machine.location_name} on {when} was "
                        "cancelled because the machine's operating hours changed."
                    ),
                    type=NotificationType.BOOKING_CANCELLED_OFFLINE,
                    kind=NotificationKind.WARN,
                    related_object_type="booking",
                    related_object_id=booking.id,
                )
            )
        if notifications:
            Notification.objects.bulk_create(notifications)
    return len(bookings)
