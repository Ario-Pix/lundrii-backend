"""
Background Tasks built on Django's own Tasks framework (``django.tasks``).

No Celery, no broker, no worker process. The backend is configured by the
``TASKS`` setting in ``core.settings.base``; by default it is
``ImmediateBackend``, which runs an enqueued Task inline.

Why bother wrapping these in Tasks at all:

* Sending mail is a network round-trip to Resend. Keeping it behind ``.enqueue()``
  marks the slow, out-of-band work explicitly, so it can be moved off the
  request cycle by changing one setting rather than rewriting call sites.
* A Task that raises is captured on its ``TaskResult`` instead of bubbling into
  the view, so a mail outage can never turn a successful registration into a 500.

Task arguments must be JSON-serializable, so pass primitives (emails, tokens,
ids) — never model instances.
"""

from __future__ import annotations

import logging

from django.tasks import task

from base.email import (
    send_login_otp_email,
    send_password_reset_email_with_token,
    send_verify_email_with_token,
)

logger = logging.getLogger(__name__)


@task
def send_login_otp_email_task(*, to: str, otp: str) -> bool:
    """Email an admin their one-time login code."""
    return send_login_otp_email(to=to, otp=otp)


@task
def send_verify_email_task(*, to: str, otp: str, token: str) -> bool:
    """Email an email-verification code plus its one-time deep link."""
    return send_verify_email_with_token(to=to, otp=otp, token=token)


@task
def send_password_reset_email_task(*, to: str, otp: str, token: str) -> bool:
    """Email a password-reset code plus its one-time deep link."""
    return send_password_reset_email_with_token(to=to, otp=otp, token=token)
