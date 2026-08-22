"""
OAuth 2.1 storage for MCP connectors.

Why this exists alongside pasted tokens: claude.ai and ChatGPT's hosted
connector UIs do not offer a "paste a secret" field. They discover an
authorization server, register themselves, and run an authorization-code flow.
Without this, "add Lundrii as a connector" in those products is impossible.

It is also the better credential. A pasted token is long-lived, copied through a
clipboard, and revoked only if the student remembers it exists. The OAuth flow
issues a short-lived access token plus a rotating refresh token, ties them to a
named client the student approved, and shows exactly what was granted.

Three rules this implementation holds to, all from OAuth 2.1:

* **PKCE is mandatory**, S256 only. These are public clients with no secret, so
  the code verifier is the only thing binding a redeemed code to the client that
  requested it.
* **Redirect URIs are matched exactly**, never by prefix — prefix matching is
  how authorization codes get stolen by an attacker-controlled path.
* **Codes and refresh tokens are single-use.** Reusing either is treated as
  theft, and the whole grant is revoked.

Secrets are stored as HMACs, same as OTPs, links and personal tokens.
"""

from __future__ import annotations

import secrets

from django.db import models
from django.utils import timezone

from authentication.services.hashing import hash_secret
from base.models import BaseModel

# Short access tokens, because a leaked one cannot be revoked out of a client's
# memory; the refresh token is the thing the student revokes.
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30
AUTHORIZATION_CODE_TTL_SECONDS = 300

MCP_SCOPE = "laundry.book"


def generate_secret(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


class OAuthClient(BaseModel):
    """
    A connector registered through RFC 7591 dynamic client registration.

    Public client: no secret is issued, because a desktop or browser client
    cannot keep one. PKCE does the work a secret would.
    """

    client_id = models.CharField(max_length=64, unique=True, db_index=True)
    client_name = models.CharField(max_length=200, blank=True)
    redirect_uris = models.JSONField(default=list)
    client_uri = models.URLField(blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "OAuth client"

    def __str__(self) -> str:
        return self.client_name or self.client_id

    def allows_redirect(self, uri: str) -> bool:
        """Exact match only. Prefix matching leaks authorization codes."""
        return uri in (self.redirect_uris or [])

    @classmethod
    def register(cls, *, client_name: str, redirect_uris: list[str], client_uri: str = ""):
        return cls.objects.create(
            client_id=generate_secret("lcli_"),
            client_name=client_name[:200],
            redirect_uris=redirect_uris,
            client_uri=client_uri[:200],
        )


class OAuthAuthorizationCode(BaseModel):
    """A one-time code handed to a client at the end of /oauth/authorize."""

    code_hash = models.CharField(max_length=64, unique=True, db_index=True)
    client = models.ForeignKey(
        OAuthClient, on_delete=models.CASCADE, related_name="codes"
    )
    student = models.ForeignKey(
        "laundry.Student", on_delete=models.CASCADE, related_name="oauth_codes"
    )
    redirect_uri = models.TextField()
    code_challenge = models.CharField(max_length=128)
    scope = models.CharField(max_length=200, default=MCP_SCOPE)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    @classmethod
    def issue(cls, *, client, student, redirect_uri, code_challenge, scope=MCP_SCOPE):
        code = generate_secret("lcod_")
        row = cls.objects.create(
            code_hash=hash_secret(code),
            client=client,
            student=student,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            scope=scope,
            expires_at=timezone.now()
            + timezone.timedelta(seconds=AUTHORIZATION_CODE_TTL_SECONDS),
        )
        return row, code

    @classmethod
    def resolve(cls, code: str):
        if not code:
            return None
        return (
            cls.objects.select_related("client", "student", "student__user")
            .filter(code_hash=hash_secret(code))
            .first()
        )

    @property
    def is_usable(self) -> bool:
        return self.consumed_at is None and self.expires_at > timezone.now()

    def consume(self) -> None:
        self.consumed_at = timezone.now()
        self.save(update_fields=["consumed_at", "updated_at"])


class OAuthRefreshToken(BaseModel):
    """
    A rotating refresh token.

    Rotation means each redemption issues a new one and retires this row. If a
    retired token is presented again, either it leaked or a client is buggy;
    either way the safe reading is theft, so the whole chain is revoked.
    """

    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    client = models.ForeignKey(
        OAuthClient, on_delete=models.CASCADE, related_name="refresh_tokens"
    )
    student = models.ForeignKey(
        "laundry.Student", on_delete=models.CASCADE, related_name="oauth_refresh_tokens"
    )
    scope = models.CharField(max_length=200, default=MCP_SCOPE)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    # Chain of rotations, so reuse of an old token can revoke every descendant.
    rotated_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="rotated_to"
    )

    class Meta:
        ordering = ("-created_at",)

    @classmethod
    def issue(cls, *, client, student, scope=MCP_SCOPE, rotated_from=None):
        token = generate_secret("lref_")
        row = cls.objects.create(
            token_hash=hash_secret(token),
            client=client,
            student=student,
            scope=scope,
            expires_at=timezone.now()
            + timezone.timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS),
            rotated_from=rotated_from,
        )
        return row, token

    @classmethod
    def resolve(cls, token: str):
        if not token:
            return None
        return (
            cls.objects.select_related("client", "student", "student__user")
            .filter(token_hash=hash_secret(token))
            .first()
        )

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now()

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at", "updated_at"])

    def revoke_chain(self) -> int:
        """
        Revoke this token and everything rotated from it.

        Called when a already-rotated token is replayed: we cannot tell the
        attacker's copy from the client's, so both are cut off and the student
        reconnects.
        """
        revoked = 0
        frontier = [self]
        while frontier:
            node = frontier.pop()
            if node.revoked_at is None:
                node.revoke()
                revoked += 1
            frontier.extend(node.rotated_to.all())
        return revoked
