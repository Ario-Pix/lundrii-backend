"""Student ticket raise + numbering."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Max
from rest_framework import status

from base.exceptions import APIError, NOT_FOUND, VALIDATION_ERROR
from base.storage import upload_ticket_photo
from laundry.models import (
    Machine,
    NotificationKind,
    NotificationType,
    Student,
    Ticket,
    TicketEvent,
    TicketKind,
    TicketStatus,
)
from laundry.services.notifications import create_in_app_notification
from laundry.services.rules import machine_is_visible


def _next_ticket_number() -> int:
    current = Ticket.objects.aggregate(m=Max("number"))["m"]
    return (current or 0) + 1


def _visible_machine(student: Student, machine_id) -> Machine:
    try:
        machine = Machine.objects.select_related("hostel", "hostel__institute").get(
            pk=machine_id,
            is_active=True,
        )
    except (Machine.DoesNotExist, ValueError, TypeError) as exc:
        raise APIError(
            NOT_FOUND,
            detail="Machine not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        ) from exc
    if not machine_is_visible(student, machine):
        raise APIError(
            NOT_FOUND,
            detail="Machine not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return machine


def raise_ticket(
    student: Student,
    *,
    kind: str,
    note: str,
    machine_id=None,
    booking_id=None,
    photo=None,
    actor=None,
) -> Ticket:
    """
    Create an open machine-not-working ticket (optional photo).

    ``booking_id`` is ignored. Suspension does not block this.
    Student API does not expose a reply thread; TicketEvent is retained for admin.
    """
    if kind != TicketKind.MAINTENANCE:
        raise APIError(
            VALIDATION_ERROR,
            detail="Only machine-not-working tickets can be raised.",
        )

    student_note = (note or "").strip()
    if not student_note:
        raise APIError(VALIDATION_ERROR, detail="note is required.")

    if machine_id is None:
        raise APIError(
            VALIDATION_ERROR,
            detail="machineId is required.",
        )

    machine = _visible_machine(student, machine_id)

    photo_url = ""
    if photo is not None:
        photo_url = upload_ticket_photo(photo)

    actor = actor or student.user

    for _ in range(5):
        try:
            with transaction.atomic():
                ticket = Ticket.objects.create(
                    student=student,
                    kind=TicketKind.MAINTENANCE,
                    status=TicketStatus.OPEN,
                    number=_next_ticket_number(),
                    machine=machine,
                    student_note=student_note,
                    photo_url=photo_url,
                )
                TicketEvent.objects.create(
                    ticket=ticket,
                    title="Raised",
                    note=student_note,
                    actor=actor,
                )
        except IntegrityError:
            continue
        break
    else:
        raise APIError(
            VALIDATION_ERROR,
            detail="Could not allocate a ticket number. Try again.",
        )

    label = f"#{ticket.number}" if ticket.number is not None else str(ticket.id)[:8]
    create_in_app_notification(
        student=student,
        title="Ticket raised",
        body=f"Ticket {label} is with the committee.",
        notification_type=NotificationType.TICKET_UPDATE,
        kind=NotificationKind.INFO,
        related_object_type="ticket",
        related_object_id=ticket.id,
        preference_field="ticket_update",
    )
    return Ticket.objects.select_related(
        "student",
        "machine",
        "machine__hostel",
        "booking",
        "recorded_holder",
    ).get(pk=ticket.pk)
