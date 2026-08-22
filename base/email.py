"""
Email abstraction: Resend HTTPS when RESEND_API_KEY is set, else console.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

# Any 6-digit run in the body is the OTP worth shouting about.
_OTP_RE = re.compile(r"\b(\d{6})\b")

#: Console deliveries are mirrored here so a developer (or a test harness) can
#: read the code without scrolling the server log.
OTP_LOG = Path(settings.BASE_DIR) / "outbox.log" if hasattr(settings, "BASE_DIR") else None


def _running_tests() -> bool:
    return "test" in sys.argv


def _from_address() -> str:
    raw = (getattr(settings, "EMAIL_FROM", "") or "").strip()
    if not raw:
        raw = "Lundrii <noreply@lundrii.app>"
    if "<" not in raw and "@" in raw:
        return f"Lundrii <{raw}>"
    return raw


def announce_otp(*, to: str, otp: str, purpose: str = "", subject: str = "") -> None:
    """Print a plaintext OTP to the runserver terminal (skipped during tests)."""
    code = (otp or "").strip()
    if not code:
        return
    if OTP_LOG is not None:
        try:
            with open(OTP_LOG, "a", encoding="utf-8") as fh:
                fh.write(
                    f"{datetime.now(timezone.utc).isoformat()}\tto={to}\t"
                    f"purpose={purpose or '-'}\tcode={code}\tsubject={subject or '-'}\n"
                )
        except OSError:
            logger.warning("Couldn't append to %s", OTP_LOG)

    logger.info("[otp] to=%s purpose=%s code=%s", to, purpose or "-", code)
    if not getattr(settings, "DEBUG", False) or _running_tests():
        return
    label = purpose.upper() if purpose else (subject or "OTP")
    print(
        "\n".join(
            [
                "",
                "=" * 66,
                f"  OTP  {label}",
                f"  to: {to}",
                f"  >>> CODE: {code} <<<",
                "=" * 66,
                "",
            ]
        ),
        flush=True,
    )


def _console_deliver(*, to: str, subject: str, body: str) -> None:
    """
    Print the email to the runserver terminal instead of sending it.

    OTP banners are emitted by send_email before this runs.
    """
    lines = [
        "",
        "=" * 66,
        f"  EMAIL (console)  to: {to}",
        f"  subject: {subject}",
        "-" * 66,
        (body or "").strip(),
        "=" * 66,
        "",
    ]
    banner = "\n".join(lines)
    if getattr(settings, "DEBUG", False) and not _running_tests():
        print(banner, flush=True)
    logger.info("[email:console] to=%s subject=%s", to, subject)


def send_email(*, to: str, subject: str, html: str, text: str | None = None) -> bool:
    """
    Send an email via Resend, or log to console when no API key is configured.

    Returns True on success (or console fallback), False on Resend failure.
    """
    from_addr = _from_address()
    api_key = getattr(settings, "RESEND_API_KEY", "") or ""
    body = text or html
    codes = _OTP_RE.findall(body or "")
    if codes:
        announce_otp(to=to, otp=codes[0], subject=subject)

    if not api_key:
        _console_deliver(to=to, subject=subject, body=body)
        return True

    try:
        import resend

        resend.api_key = api_key
        params: dict = {
            "from": from_addr,
            "to": [to],
            "subject": subject,
            "html": html,
        }
        if text:
            params["text"] = text
        resend.Emails.send(params)
        return True
    except Exception:
        logger.exception("Failed to send email via Resend to %s (from=%s)", to, from_addr)
        return False


def frontend_url() -> str:
    return getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")


def build_verify_email_link(token: str) -> str:
    """Deep link for email verification (`/auth/verify?token=`)."""
    return f"{frontend_url()}/auth/verify?token={token}"


def build_reset_password_link(token: str) -> str:
    """Deep link for password reset (`/auth/reset?token=`)."""
    return f"{frontend_url()}/auth/reset?token={token}"


def _render_email(name: str, context: dict) -> tuple[str, str]:
    html = render_to_string(f"emails/{name}.html", context)
    text = render_to_string(f"emails/{name}.txt", context)
    return html, text


def send_login_otp_email(*, to: str, otp: str) -> bool:
    subject = "Your Lundrii login code"
    html, text = _render_email("login_otp", {"otp": otp})
    return send_email(to=to, subject=subject, html=html, text=text)


def send_verify_email(*, to: str, otp: str, link: str) -> bool:
    subject = "Verify your Lundrii email"
    html, text = _render_email("verify_email", {"otp": otp, "link": link})
    return send_email(to=to, subject=subject, html=html, text=text)


def send_password_reset_email(*, to: str, otp: str, link: str) -> bool:
    subject = "Reset your Lundrii password"
    html, text = _render_email("password_reset", {"otp": otp, "link": link})
    return send_email(to=to, subject=subject, html=html, text=text)


def send_verify_email_with_token(*, to: str, otp: str, token: str) -> bool:
    """Wave 2a helper: build verify URL from a cached one-time token."""
    return send_verify_email(to=to, otp=otp, link=build_verify_email_link(token))


def send_password_reset_email_with_token(*, to: str, otp: str, token: str) -> bool:
    """Wave 2a helper: build reset URL from a cached one-time token."""
    return send_password_reset_email(to=to, otp=otp, link=build_reset_password_link(token))
