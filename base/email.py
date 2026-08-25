"""
Email abstraction: Resend HTTPS when RESEND_API_KEY is set, else console.
"""

from __future__ import annotations

import json
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
    """Return a configured From, or empty when neither env alias is set.

    Does not fall back to noreply@lundrii.app. Callers that talk to Resend
    must treat an empty result as a configuration error.
    """
    raw = (getattr(settings, "EMAIL_FROM", "") or "").strip()
    if not raw:
        raw = (getattr(settings, "RESEND_FROM_EMAIL", "") or "").strip()
    if not raw:
        return ""
    if "<" not in raw and "@" in raw:
        return f"Lundrii <{raw}>"
    return raw


def _as_mapping(response: object) -> dict:
    if response is None:
        return {}
    if isinstance(response, dict):
        return dict(response)
    dumped = getattr(response, "model_dump", None)
    if callable(dumped):
        try:
            data = dumped()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    data: dict = {}
    for key in ("id", "error", "message", "name", "statusCode"):
        if hasattr(response, key):
            try:
                data[key] = getattr(response, key)
            except Exception:
                continue
    return data


def _format_resend_detail(value: object) -> str:
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _interpret_resend_response(response: object) -> tuple[bool, str]:
    """Success only when Resend returned an id and no error payload."""
    data = _as_mapping(response)
    error = data.get("error")
    if error not in (None, "", {}, []):
        return False, _format_resend_detail(error)
    email_id = data.get("id")
    if email_id:
        return True, str(email_id)
    if response is None:
        return False, "empty Resend response"
    return False, _format_resend_detail(data or response)


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
    api_key = getattr(settings, "RESEND_API_KEY", "") or ""
    body = text or html
    codes = _OTP_RE.findall(body or "")
    if codes:
        announce_otp(to=to, otp=codes[0], subject=subject)

    if not api_key:
        _console_deliver(to=to, subject=subject, body=body)
        return True

    from_addr = _from_address()
    if not from_addr:
        logger.error(
            "RESEND_API_KEY is set but EMAIL_FROM / RESEND_FROM_EMAIL is empty; "
            "refusing silent fallback to noreply@lundrii.app (to=%s)",
            to,
        )
        return False

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
        response = resend.Emails.send(params)
    except Exception:
        logger.exception("Failed to send email via Resend to %s (from=%s)", to, from_addr)
        return False

    ok, detail = _interpret_resend_response(response)
    if ok:
        logger.info("[email:resend] to=%s from=%s id=%s", to, from_addr, detail)
        return True
    logger.error("[email:resend] to=%s from=%s error=%s", to, from_addr, detail)
    return False


def frontend_url() -> str:
    return getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")


def admin_frontend_url() -> str:
    return getattr(settings, "ADMIN_FRONTEND_URL", frontend_url()).rstrip("/")


def build_reset_password_link(token: str, *, is_admin: bool = False) -> str:
    """Deep link for password reset (`/auth/reset?token=`)."""
    origin = admin_frontend_url() if is_admin else frontend_url()
    return f"{origin}/auth/reset?token={token}"


def _render_email(name: str, context: dict) -> tuple[str, str]:
    # No request → Context, not RequestContext. Auth context processors
    # (and template `user`) cannot leak into the body.
    html = render_to_string(f"emails/{name}.html", context)
    text = render_to_string(f"emails/{name}.txt", context)
    return html, text


def resolve_email_name(*, name: str | None = None, email: str | None = None) -> str:
    """
    Greeting name for email copy.

    Prefer a real display name; otherwise the email local-part; otherwise
    ``there`` so templates can safely say ``Hi {{ name }},``.
    """
    cleaned = (name or "").strip()
    if cleaned:
        return cleaned
    local = ((email or "").split("@", 1)[0] or "").strip()
    if local:
        return local
    return "there"


def personalized_subject(base: str, name: str) -> str:
    """Prefix ``base`` with the greeting name when it is more than a fallback."""
    cleaned = (name or "").strip()
    if not cleaned or cleaned == "there":
        return base
    if not base:
        return cleaned
    return f"{cleaned}, {base[0].lower()}{base[1:]}"


def send_login_otp_email(*, to: str, otp: str, name: str = "") -> bool:
    greeting = resolve_email_name(name=name, email=to)
    subject = personalized_subject("Your Lundrii login code", greeting)
    html, text = _render_email("login_otp", {"otp": otp, "name": greeting})
    return send_email(to=to, subject=subject, html=html, text=text)


def send_password_reset_email(*, to: str, otp: str, link: str, name: str = "") -> bool:
    greeting = resolve_email_name(name=name, email=to)
    subject = personalized_subject("Reset your Lundrii password", greeting)
    html, text = _render_email(
        "password_reset", {"otp": otp, "link": link, "name": greeting}
    )
    return send_email(to=to, subject=subject, html=html, text=text)


def send_password_reset_email_with_token(
    *, to: str, otp: str, token: str, is_admin: bool = False, name: str = ""
) -> bool:
    """Wave 2a helper: build reset URL from a cached one-time token."""
    return send_password_reset_email(
        to=to,
        otp=otp,
        link=build_reset_password_link(token, is_admin=is_admin),
        name=name,
    )


def send_booking_confirmed_email(
    *,
    to: str,
    name: str = "",
    machine: str,
    hostel: str,
    when: str,
) -> bool:
    """Transactional receipt after a successful booking create."""
    greeting = resolve_email_name(name=name, email=to)
    subject = personalized_subject("Your laundry slot is booked", greeting)
    html, text = _render_email(
        "booking_confirmed",
        {
            "name": greeting,
            "machine": machine,
            "hostel": hostel,
            "when": when,
        },
    )
    return send_email(to=to, subject=subject, html=html, text=text)
