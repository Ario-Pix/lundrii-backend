"""In-app notification helpers (no WhatsApp / email channels)."""

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from rest_framework.exceptions import NotFound

from laundry.models import Notification, NotificationKind, NotificationPreference, NotificationType


def student_allows_notification(student, preference_field: str) -> bool:
    try:
        prefs = student.notification_preferences
    except (ObjectDoesNotExist, AttributeError):
        return True
    if prefs is None:
        return True
    return bool(getattr(prefs, preference_field, True))


def create_in_app_notification(
    *,
    student,
    title: str,
    body: str,
    notification_type: str,
    kind: str = NotificationKind.INFO,
    related_object_type: str = "",
    related_object_id=None,
    preference_field: str | None = None,
) -> Notification | None:
    if preference_field and not student_allows_notification(student, preference_field):
        return None
    return Notification.objects.create(
        student=student,
        title=title,
        body=body,
        type=notification_type,
        kind=kind,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
    )


def get_or_create_preferences(student) -> NotificationPreference:
    prefs, _ = NotificationPreference.objects.get_or_create(student=student)
    return prefs


def notification_deep_link(notification: Notification) -> str | None:
    rel = (notification.related_object_type or "").strip().lower()
    oid = notification.related_object_id
    if rel == "booking":
        return f"/bookings/{oid}" if oid else "/bookings"
    if rel == "exchange":
        return f"/exchange/{oid}" if oid else "/exchange"
    if rel == "ticket":
        return f"/tickets/{oid}" if oid else "/tickets"
    if rel in {"strike", "suspension"}:
        return "/profile"

    ntype = notification.type
    if ntype in {
        NotificationType.BOOKING_CONFIRMED,
        NotificationType.SLOT_REMINDER,
        NotificationType.BOOKING_CANCELLED_OFFLINE,
    }:
        return "/bookings"
    if ntype in {NotificationType.EXCHANGE_REQUEST, NotificationType.EXCHANGE_OUTCOME}:
        return "/exchange"
    if ntype == NotificationType.TICKET_UPDATE:
        return "/tickets"
    if ntype in {NotificationType.STRIKE, NotificationType.SUSPENSION}:
        return "/profile"
    return None


def mark_notification_read(student, notification_id) -> Notification:
    try:
        notification = Notification.objects.get(
            pk=notification_id,
            student=student,
            is_active=True,
        )
    except (Notification.DoesNotExist, ValueError, TypeError) as exc:
        raise NotFound("Notification not found.") from exc
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at", "updated_at"])
    return notification


def mark_all_notifications_read(student) -> int:
    now = timezone.now()
    return Notification.objects.filter(
        student=student,
        is_active=True,
        read_at__isnull=True,
    ).update(read_at=now, updated_at=now)
