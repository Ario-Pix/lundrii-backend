"""
One-time verify-email and password-reset tokens in cache.

Keys (token in the key is hashed; value is user_id):
  auth:link:verify:{token_hash} -> user_id
  auth:link:reset:{token_hash}  -> user_id
"""

from __future__ import annotations

import secrets
from enum import Enum
from uuid import UUID

from django.conf import settings
from django.core.cache import cache

from authentication.services.hashing import hash_secret


class LinkPurpose(str, Enum):
    VERIFY = "verify"
    RESET = "reset"


def _purpose(purpose: str | LinkPurpose) -> str:
    value = purpose.value if isinstance(purpose, LinkPurpose) else str(purpose)
    value = value.strip().lower()
    try:
        return LinkPurpose(value).value
    except ValueError as exc:
        raise ValueError(f"Unknown link purpose: {purpose!r}") from exc


def _user_id_str(user_id: str | UUID) -> str:
    value = str(user_id).strip()
    if not value:
        raise ValueError("user_id is required")
    return value


def link_ttl_seconds(purpose: str | LinkPurpose) -> int:
    purpose = _purpose(purpose)
    if purpose == LinkPurpose.VERIFY.value:
        return int(getattr(settings, "VERIFY_LINK_TTL_SECONDS", 1800))
    return int(getattr(settings, "RESET_LINK_TTL_SECONDS", 3600))


def link_cache_key(purpose: str | LinkPurpose, token: str) -> str:
    """Key uses the hashed token so plaintext never appears in cache."""
    return f"auth:link:{_purpose(purpose)}:{hash_secret(token)}"


def create_link(user_id: str | UUID, purpose: str | LinkPurpose) -> str:
    """
    Create a one-time link token. Returns the plaintext token for the URL.

    Only the HMAC of the token is used as the cache key; value is user_id.
    """
    purpose = _purpose(purpose)
    token = secrets.token_urlsafe(32)
    cache.set(
        link_cache_key(purpose, token),
        _user_id_str(user_id),
        timeout=link_ttl_seconds(purpose),
    )
    return token


def consume_link(token: str, purpose: str | LinkPurpose) -> str | None:
    """
    Validate and delete a one-time token.

    Returns user_id string on success, or None if missing/expired/invalid.
    """
    purpose = _purpose(purpose)
    raw = (token or "").strip()
    if not raw:
        return None
    key = link_cache_key(purpose, raw)
    user_id = cache.get(key)
    if not user_id:
        return None
    cache.delete(key)
    return str(user_id)


def delete_link(token: str, purpose: str | LinkPurpose) -> None:
    raw = (token or "").strip()
    if not raw:
        return
    cache.delete(link_cache_key(purpose, raw))


def create_verify_link(user_id: str | UUID) -> str:
    return create_link(user_id, LinkPurpose.VERIFY)


def create_reset_link(user_id: str | UUID) -> str:
    return create_link(user_id, LinkPurpose.RESET)


def consume_verify_link(token: str) -> str | None:
    return consume_link(token, LinkPurpose.VERIFY)


def consume_reset_link(token: str) -> str | None:
    return consume_link(token, LinkPurpose.RESET)
