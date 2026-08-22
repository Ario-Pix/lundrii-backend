"""HMAC helpers so OTPs and link tokens are never stored in plaintext."""

from __future__ import annotations

import hashlib
import hmac

from django.conf import settings


def hash_secret(value: str) -> str:
    key = settings.SECRET_KEY.encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def secrets_match(plaintext: str, stored_hash: str) -> bool:
    if not plaintext or not stored_hash:
        return False
    return hmac.compare_digest(hash_secret(plaintext), stored_hash)
