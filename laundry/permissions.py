"""Institute scoping helpers for admin APIs + student mutation gate."""

from django.core.exceptions import ObjectDoesNotExist
from rest_framework.permissions import SAFE_METHODS

from base.permissions import IsStudent, user_is_super_administrator
from laundry.services.access import assert_can_mutate


class IsStudentCanMutate(IsStudent):
    """
    Student who may create, edit, or exchange bookings.

    Unsafe methods run ``assert_can_mutate`` (UNVERIFIED / SUSPENDED).
    Safe methods stay readable while suspended (history / browse).

    Use on exchange create / approve (Wave 3a). Do **not** use on ticket
    raise — reporting problems while suspended is allowed.
    """

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in SAFE_METHODS:
            return True
        assert_can_mutate(request.user.student)
        return True


def scoped_institute_id(user):
    """
    Institute UUID for an administrator; ``None`` for super admins (unscoped).

    Super-admin takes precedence if a user somehow has both profiles.
    """
    if user_is_super_administrator(user):
        return None
    try:
        admin = user.administrator
    except (ObjectDoesNotExist, AttributeError):
        return None
    if admin is None or not admin.is_active:
        return None
    return admin.institute_id
