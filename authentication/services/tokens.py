"""JWT helpers wrapping simplejwt RefreshToken.for_user."""

from __future__ import annotations

from typing import Any

from rest_framework_simplejwt.tokens import RefreshToken

from base.clients import JWT_CLIENT_CLAIM


def issue_jwt_pair(user: Any, *, client: str | None = None) -> dict[str, str]:
    """
    Issue access + refresh JWTs for a user.

    Wave 2a: call after successful login OTP verify (and optionally reset).
    Header: Authorization: Bearer <access>

    ``client`` records which app the login came from (see ``base.clients``). It
    is stamped on the refresh token, and simplejwt copies non-reserved claims
    onto every access token derived from it — including the ones minted later by
    ``/auth/refresh`` — so the client survives for the whole session without the
    API ever asking for it again.
    """
    refresh = RefreshToken.for_user(user)
    if client:
        refresh[JWT_CLIENT_CLAIM] = client
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }
