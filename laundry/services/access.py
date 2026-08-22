"""
Shared student access gates for booking / exchange mutations.

Wave 3a exchanges must call ``assert_can_mutate`` (or use ``IsStudentCanMutate``)
on create / approve / reject-as-holder flows that transfer slots.

Do **not** call this for ticket raise or ticket list — suspended students may
still browse, view history, and report problems (product rule 36).
"""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone
from rest_framework import status

from base.exceptions import APIError, SUSPENDED, UNVERIFIED
from laundry.models import Student


def _aware(dt: datetime) -> datetime:
    if timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt, timezone.get_current_timezone())


def assert_can_mutate(student: Student, *, now: datetime | None = None) -> None:
    """
    Block create / edit / exchange when unverified or suspended.

    Raises ``APIError`` with ``UNVERIFIED`` or ``SUSPENDED`` (+ ``clearsAt``).
    """
    now = _aware(now or timezone.now())
    if not student.is_email_verified:
        raise APIError(
            UNVERIFIED,
            detail="Confirm your email before booking.",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if student.suspension_ends is not None and now < student.suspension_ends:
        raise APIError(
            SUSPENDED,
            detail=(
                "You cannot create, edit, or exchange bookings while your "
                "account is suspended."
            ),
            status_code=status.HTTP_403_FORBIDDEN,
            extra={"clearsAt": student.suspension_ends.isoformat()},
        )
