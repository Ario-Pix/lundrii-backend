"""
Recording what administrators did.

The portal spec asks for "a read-only log of every administrator action,
showing who did what, to whom or what, and when", which "cannot be edited or
deleted". Two design consequences follow:

* **Write-only from the app's point of view.** Nothing here updates or deletes.
  There is no revoke, no correction — a wrong entry is answered by a later
  entry, never by rewriting history.
* **Labels are copied, not joined.** ``actor_label`` and ``target_label`` are
  frozen at write time so the log still reads correctly after a machine is
  retired or an administrator is renamed. A log that silently rewrites itself
  when the world moves on cannot be used to answer "what did we do, and when".

``record`` never raises. An audit write failing must not roll back the action it
was describing — losing one log line is bad, losing the operator's actual work
because logging failed is worse. Failures are logged instead.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ObjectDoesNotExist

from laundry.models import AdminAuditLog

logger = logging.getLogger(__name__)

Action = AdminAuditLog.Action


def actor_for(user):
    """The Administrator profile behind a request user, or None."""
    try:
        admin = user.administrator
    except (ObjectDoesNotExist, AttributeError):
        return None
    return admin if admin and admin.is_active else None


def actor_label_for(user) -> str:
    admin = actor_for(user)
    if admin is not None:
        return admin.display_name or getattr(user, "email", "administrator")
    try:
        return user.superadministrator.display_name or "Platform"
    except (ObjectDoesNotExist, AttributeError):
        return getattr(user, "email", "system")


def record(
    *,
    user,
    action: str,
    summary: str,
    target=None,
    target_type: str = "",
    target_label: str = "",
    institute=None,
    metadata: dict | None = None,
) -> AdminAuditLog | None:
    """Append one entry. Returns None if writing failed (never raises)."""
    try:
        admin = actor_for(user)
        if institute is None and admin is not None:
            institute = admin.institute

        if target is not None:
            target_type = target_type or type(target).__name__.lower()
            target_label = target_label or str(target)[:200]

        return AdminAuditLog.objects.create(
            institute=institute,
            actor=admin,
            actor_label=actor_label_for(user)[:200],
            action=action,
            target_type=target_type[:40],
            target_id=getattr(target, "pk", None),
            target_label=target_label[:200],
            summary=summary[:400],
            metadata=metadata or {},
        )
    except Exception:
        logger.exception("Failed to write audit entry action=%s", action)
        return None
