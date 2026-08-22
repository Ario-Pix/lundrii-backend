"""
Connector tokens: how a student's ChatGPT / Claude account proves who it is.

A student mints a token in the app and pastes it into the assistant. The token
is a bearer credential for the MCP endpoint and nothing else — it can only
reach the tools in `mcp_server/tools.py`, always acting as the student who
created it, and always through the same booking services the mobile app uses.

Only an HMAC of the token is stored (the same `hash_secret` used for OTPs and
one-time links), so a database leak does not hand out working credentials. The
plaintext is returned exactly once, at creation.
"""

from __future__ import annotations

import secrets

from django.db import models
from django.utils import timezone

from authentication.services.hashing import hash_secret
from base.models import BaseModel

TOKEN_PREFIX = "lmcp_"
TOKEN_ENTROPY_BYTES = 32
# Enough of the token to recognise it in a list, never enough to use it.
DISPLAY_CHARS = 6


def generate_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)}"


class McpTokenQuerySet(models.QuerySet):
    def usable(self, *, now=None):
        now = now or timezone.now()
        return self.filter(
            is_active=True,
            revoked_at__isnull=True,
            student__is_active=True,
            student__user__is_active=True,
        ).filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))


class McpToken(BaseModel):
    """
    A bearer credential for `/mcp/`.

    Two kinds share this table, because both are just "a hashed secret that
    identifies one student to the MCP endpoint":

    * **personal** — minted by the student and pasted into a local client. Long
      lived, no expiry unless asked for.
    * **oauth** — issued by the token endpoint at the end of an authorization
      code flow, tied to the approved client and expiring in an hour.
    """

    student = models.ForeignKey(
        "laundry.Student",
        on_delete=models.CASCADE,
        related_name="mcp_tokens",
    )
    oauth_client = models.ForeignKey(
        "mcp_server.OAuthClient",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="access_tokens",
        help_text="Set when this token came from the OAuth flow rather than being pasted.",
    )
    name = models.CharField(
        max_length=100,
        help_text="What this token is for, e.g. 'Claude desktop'.",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    token_hint = models.CharField(
        max_length=32,
        help_text="Leading characters, shown so a student can tell tokens apart.",
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    objects = McpTokenQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "MCP token"
        verbose_name_plural = "MCP tokens"

    def __str__(self) -> str:
        return f"{self.name} ({self.token_hint})"

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_usable(self) -> bool:
        return self.is_active and not self.is_revoked and not self.is_expired

    @classmethod
    def issue(cls, *, student, name: str, expires_at=None) -> tuple["McpToken", str]:
        """Create a token, returning the row and the plaintext (shown once)."""
        plaintext = generate_token()
        token = cls.objects.create(
            student=student,
            name=name,
            token_hash=hash_secret(plaintext),
            token_hint=f"{TOKEN_PREFIX}{plaintext[len(TOKEN_PREFIX):][:DISPLAY_CHARS]}…",
            expires_at=expires_at,
        )
        return token, plaintext

    @property
    def is_oauth(self) -> bool:
        return self.oauth_client_id is not None

    @classmethod
    def issue_for_oauth(cls, *, student, client, scope: str) -> tuple["McpToken", str]:
        """Mint a short-lived access token at the end of an OAuth exchange."""
        from mcp_server.oauth_models import ACCESS_TOKEN_TTL_SECONDS

        plaintext = generate_token()
        token = cls.objects.create(
            student=student,
            oauth_client=client,
            name=(client.client_name or "OAuth connector")[:100],
            token_hash=hash_secret(plaintext),
            token_hint=f"{TOKEN_PREFIX}{plaintext[len(TOKEN_PREFIX):][:DISPLAY_CHARS]}…",
            expires_at=timezone.now()
            + timezone.timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS),
        )
        return token, plaintext

    @classmethod
    def resolve(cls, plaintext: str) -> "McpToken | None":
        """Look up a usable token by its plaintext, or None."""
        if not plaintext or not plaintext.startswith(TOKEN_PREFIX):
            return None
        return (
            cls.objects.usable()
            .select_related(
                "student", "student__user", "student__institute", "oauth_client"
            )
            .filter(token_hash=hash_secret(plaintext))
            .first()
        )

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at", "updated_at"])

    def touch(self) -> None:
        """Record use, cheaply — no full save, no updated_at churn."""
        now = timezone.now()
        type(self).objects.filter(pk=self.pk).update(last_used_at=now)
        self.last_used_at = now


# Re-exported so `from mcp_server.models import OAuthClient` works and Django
# picks the OAuth models up from this app's models module.
from mcp_server.oauth_models import (  # noqa: E402,F401
    ACCESS_TOKEN_TTL_SECONDS,
    AUTHORIZATION_CODE_TTL_SECONDS,
    MCP_SCOPE,
    REFRESH_TOKEN_TTL_SECONDS,
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthRefreshToken,
)
