"""
Cache-backed hashed OTPs (login / verify-email / reset).

Keys (never store plaintext OTPs):
  auth:otp:{purpose}:{email}        hashed 6-digit OTP
  auth:rate:{purpose}:{email}       send counter (window)
  auth:attempts:{purpose}:{email}   failed verify counter
  auth:cooldown:{purpose}:{email}   resend cooldown (value = unix expiry)
"""

from __future__ import annotations

import secrets
import time
from enum import Enum

from django.conf import settings
from django.core.cache import cache

from authentication.services.hashing import hash_secret, secrets_match
from base.email import announce_otp


class OtpPurpose(str, Enum):
    LOGIN = "login"
    VERIFY = "verify"
    RESET = "reset"


class AuthServiceError(Exception):
    """Base for auth cache-service errors."""

    def __init__(self, message: str = "", *, retry_after: int = 0):
        self.retry_after = retry_after
        super().__init__(message or self.__class__.__name__)


class OtpRateLimited(AuthServiceError):
    """Too many OTP/link emails in the rate-limit window."""


class OtpCooldown(AuthServiceError):
    """Resend cooldown still active (~60s, Flutter-aligned)."""


class OtpLocked(AuthServiceError):
    """Too many failed verification attempts; OTP invalidated."""


def _purpose(purpose: str | OtpPurpose) -> str:
    value = purpose.value if isinstance(purpose, OtpPurpose) else str(purpose)
    value = value.strip().lower()
    try:
        return OtpPurpose(value).value
    except ValueError as exc:
        raise ValueError(f"Unknown OTP purpose: {purpose!r}") from exc


def _email(email: str) -> str:
    return (email or "").strip().lower()


def otp_cache_key(purpose: str | OtpPurpose, email: str) -> str:
    return f"auth:otp:{_purpose(purpose)}:{_email(email)}"


def rate_cache_key(purpose: str | OtpPurpose, email: str) -> str:
    return f"auth:rate:{_purpose(purpose)}:{_email(email)}"


def attempts_cache_key(purpose: str | OtpPurpose, email: str) -> str:
    return f"auth:attempts:{_purpose(purpose)}:{_email(email)}"


def cooldown_cache_key(purpose: str | OtpPurpose, email: str) -> str:
    return f"auth:cooldown:{_purpose(purpose)}:{_email(email)}"


def otp_ttl_seconds(purpose: str | OtpPurpose) -> int:
    purpose = _purpose(purpose)
    mapping = {
        OtpPurpose.LOGIN.value: int(getattr(settings, "OTP_TTL_SECONDS", 600)),
        OtpPurpose.VERIFY.value: int(getattr(settings, "OTP_VERIFY_TTL_SECONDS", 1800)),
        OtpPurpose.RESET.value: int(getattr(settings, "OTP_RESET_TTL_SECONDS", 3600)),
    }
    return mapping[purpose]


def _max_attempts() -> int:
    return int(getattr(settings, "OTP_MAX_ATTEMPTS", 5))


def _rate_limit_max() -> int:
    return int(getattr(settings, "OTP_RATE_LIMIT_MAX", 5))


def _rate_window_seconds() -> int:
    return int(getattr(settings, "OTP_RATE_LIMIT_WINDOW_SECONDS", 900))


def _cooldown_seconds() -> int:
    return int(getattr(settings, "OTP_COOLDOWN_SECONDS", 60))


def _incr_with_ttl(key: str, ttl: int) -> int:
    if cache.add(key, 1, timeout=ttl):
        return 1
    try:
        return int(cache.incr(key))
    except ValueError:
        cache.set(key, 1, timeout=ttl)
        return 1


def otp_send_cooldown_remaining(email: str, purpose: str | OtpPurpose) -> int:
    """Seconds left on resend cooldown, or 0 if none."""
    expires_at = cache.get(cooldown_cache_key(purpose, email))
    if expires_at is None:
        return 0
    try:
        remaining = int(float(expires_at) - time.time())
    except (TypeError, ValueError):
        return 0
    return max(0, remaining)


def record_otp_send(email: str, purpose: str | OtpPurpose) -> None:
    """
    Apply rate-limit + resend cooldown for an auth email send.

    Raises OtpCooldown / OtpRateLimited. create_otp() calls this by default;
    call it alone for opaque flows (e.g. unknown email on forgot-password).
    """
    purpose = _purpose(purpose)
    email = _email(email)

    remaining = otp_send_cooldown_remaining(email, purpose)
    if remaining > 0:
        raise OtpCooldown("OTP resend cooldown active.", retry_after=remaining)

    rate_key = rate_cache_key(purpose, email)
    current = cache.get(rate_key) or 0
    try:
        current = int(current)
    except (TypeError, ValueError):
        current = 0
    if current >= _rate_limit_max():
        raise OtpRateLimited(
            "OTP send rate limited.",
            retry_after=_rate_window_seconds(),
        )

    _incr_with_ttl(rate_key, _rate_window_seconds())
    ttl = _cooldown_seconds()
    cache.set(cooldown_cache_key(purpose, email), time.time() + ttl, timeout=ttl)


def create_otp(
    email: str,
    purpose: str | OtpPurpose,
    *,
    record_send: bool = True,
) -> str:
    """
    Generate a 6-digit OTP, store only its hash, return plaintext for emailing.

    Single outstanding OTP per purpose+email (overwrites previous).
    Resets the failed-attempt counter. Enforces cooldown + rate limit unless
    record_send=False (then call record_otp_send yourself).
    """
    purpose = _purpose(purpose)
    email = _email(email)
    if not email:
        raise ValueError("email is required")

    if record_send:
        record_otp_send(email, purpose)

    otp = f"{secrets.randbelow(1_000_000):06d}"
    cache.set(otp_cache_key(purpose, email), hash_secret(otp), timeout=otp_ttl_seconds(purpose))
    cache.delete(attempts_cache_key(purpose, email))
    announce_otp(to=email, otp=otp, purpose=purpose)
    return otp


def verify_otp(email: str, otp: str, purpose: str | OtpPurpose) -> bool:
    """
    Check a submitted OTP. Deletes the cache entry on success (single-use).

    Returns False if missing, expired, or wrong.
    Raises OtpLocked after OTP_MAX_ATTEMPTS failures (OTP is invalidated).
    """
    purpose = _purpose(purpose)
    email = _email(email)
    code = (otp or "").strip()

    attempts_key = attempts_cache_key(purpose, email)
    otp_key = otp_cache_key(purpose, email)
    max_attempts = _max_attempts()
    ttl = otp_ttl_seconds(purpose)

    attempts = cache.get(attempts_key) or 0
    try:
        attempts = int(attempts)
    except (TypeError, ValueError):
        attempts = 0
    if attempts >= max_attempts:
        raise OtpLocked("Too many failed OTP attempts.", retry_after=ttl)

    stored_hash = cache.get(otp_key)
    if not stored_hash or not code:
        return False

    if secrets_match(code, str(stored_hash)):
        cache.delete(otp_key)
        cache.delete(attempts_key)
        return True

    new_attempts = _incr_with_ttl(attempts_key, ttl)
    if new_attempts >= max_attempts:
        cache.delete(otp_key)
        raise OtpLocked("Too many failed OTP attempts.", retry_after=ttl)
    return False


def delete_otp(email: str, purpose: str | OtpPurpose) -> None:
    """Remove a cached OTP and its attempt counter (not rate/cooldown)."""
    purpose = _purpose(purpose)
    email = _email(email)
    cache.delete(otp_cache_key(purpose, email))
    cache.delete(attempts_cache_key(purpose, email))
