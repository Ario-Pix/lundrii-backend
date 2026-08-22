"""
Role-gated DRF permissions.

Roles live as OneToOne profiles on ``base.BaseUser``:
``user.administrator`` / ``user.superadministrator`` (laundry app).
"""

from django.core.exceptions import ObjectDoesNotExist
from rest_framework.permissions import BasePermission


def _has_active_role(user, related_name: str) -> bool:
    try:
        profile = getattr(user, related_name)
    except (ObjectDoesNotExist, AttributeError):
        return False
    return bool(profile is not None and getattr(profile, "is_active", False))


def user_is_administrator(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    return _has_active_role(user, "administrator")


def user_is_super_administrator(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    return _has_active_role(user, "superadministrator")


def user_is_student(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    return _has_active_role(user, "student")


class IsAdministrator(BasePermission):
    """Allow access only to users with an active Administrator profile."""

    def has_permission(self, request, view):
        return user_is_administrator(request.user)


class IsSuperAdministrator(BasePermission):
    """Allow access only to users with an active SuperAdministrator profile."""

    def has_permission(self, request, view):
        return user_is_super_administrator(request.user)


class IsAdministratorOrSuperAdministrator(BasePermission):
    """Hostel committee or platform operator."""

    def has_permission(self, request, view):
        user = request.user
        return user_is_super_administrator(user) or user_is_administrator(user)


class IsStudent(BasePermission):
    """
    Allow access only to users with an active Student profile.

    Browse, history, notifications, and ticket raise stay on this class.
    Booking / exchange mutations must also call
    ``laundry.services.access.assert_can_mutate`` or use
    ``laundry.permissions.IsStudentCanMutate`` (Wave 3a).
    """

    def has_permission(self, request, view):
        return user_is_student(request.user)
