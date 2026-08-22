"""
The OAuth 2.1 authorization server that fronts the MCP endpoint.

The dance a hosted connector performs, and what serves each step:

    GET  /.well-known/oauth-protected-resource  which AS guards /mcp/   (RFC 9728)
    GET  /.well-known/oauth-authorization-server  where the endpoints are (RFC 8414)
    POST /oauth/register                        client registers itself  (RFC 7591)
    GET  /oauth/authorize                       student signs in, approves
    POST /oauth/authorize                       approval -> redirect with code
    POST /oauth/token                           code + verifier -> tokens

Error style differs by endpoint on purpose, and it is not arbitrary: an invalid
`client_id` or `redirect_uri` must render an error *to the student* rather than
redirect, because an unvalidated redirect target is exactly the open redirect an
attacker wants. Everything after those two are validated redirects with
`error=` in the query, per RFC 6749 §4.1.2.1.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from urllib.parse import urlencode, urlparse

from django.contrib.auth import authenticate
from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status

from laundry.models import Student
from mcp_server.models import (
    MCP_SCOPE,
    McpToken,
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthRefreshToken,
)
from mcp_server.oauth_models import ACCESS_TOKEN_TTL_SECONDS

logger = logging.getLogger(__name__)

SUPPORTED_RESPONSE_TYPES = ["code"]
SUPPORTED_GRANT_TYPES = ["authorization_code", "refresh_token"]
# S256 only. OAuth 2.1 removes "plain", which offers no protection at all.
SUPPORTED_CODE_CHALLENGE_METHODS = ["S256"]


def _issuer(request) -> str:
    return f"{request.scheme}://{request.get_host()}"


def _oauth_error(error: str, description: str, http_status: int = 400) -> JsonResponse:
    return JsonResponse(
        {"error": error, "error_description": description}, status=http_status
    )


def _redirect_with_error(redirect_uri: str, error: str, description: str, state=None):
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    joiner = "&" if urlparse(redirect_uri).query else "?"
    return HttpResponseRedirect(f"{redirect_uri}{joiner}{urlencode(params)}")


def verify_pkce(verifier: str, challenge: str) -> bool:
    """S256: BASE64URL(SHA256(verifier)) == challenge, unpadded."""
    if not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    # Not secrets.compare_digest-worthy (the challenge is public), but cheap.
    return expected == challenge


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class ProtectedResourceMetadata(View):
    """RFC 9728 — tells a client which authorization server guards /mcp/."""

    def get(self, request):
        issuer = _issuer(request)
        return JsonResponse(
            {
                "resource": f"{issuer}/mcp/",
                "authorization_servers": [issuer],
                "scopes_supported": [MCP_SCOPE],
                "bearer_methods_supported": ["header"],
            }
        )


class AuthorizationServerMetadata(View):
    """RFC 8414 — the endpoint map a connector reads before doing anything."""

    def get(self, request):
        issuer = _issuer(request)
        return JsonResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}{reverse('oauth-authorize')}",
                "token_endpoint": f"{issuer}{reverse('oauth-token')}",
                "registration_endpoint": f"{issuer}{reverse('oauth-register')}",
                "response_types_supported": SUPPORTED_RESPONSE_TYPES,
                "grant_types_supported": SUPPORTED_GRANT_TYPES,
                "code_challenge_methods_supported": SUPPORTED_CODE_CHALLENGE_METHODS,
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": [MCP_SCOPE],
            }
        )


# ---------------------------------------------------------------------------
# Dynamic client registration
# ---------------------------------------------------------------------------


@method_decorator(csrf_exempt, name="dispatch")
class RegisterClient(View):
    """
    RFC 7591. Open registration, which is what hosted connectors require.

    That is safe here only because registering grants nothing: a client still
    cannot act until a student signs in and approves it, and the client_id it
    receives is not a credential.
    """

    def post(self, request):
        try:
            payload = json.loads(request.body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _oauth_error("invalid_request", "Body must be JSON.")

        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _oauth_error("invalid_redirect_uri", "redirect_uris is required.")

        for uri in redirect_uris:
            if not isinstance(uri, str) or not uri.strip():
                return _oauth_error("invalid_redirect_uri", "Malformed redirect_uri.")
            parsed = urlparse(uri)
            # Allow custom schemes (native apps) and localhost, but a remote
            # http:// target would send the code over the wire in clear.
            if parsed.scheme == "http" and parsed.hostname not in (
                "localhost",
                "127.0.0.1",
                "::1",
            ):
                return _oauth_error(
                    "invalid_redirect_uri",
                    "http redirect URIs are only allowed on localhost; use https.",
                )

        client = OAuthClient.register(
            client_name=str(payload.get("client_name") or "Unnamed connector"),
            redirect_uris=[u.strip() for u in redirect_uris],
            client_uri=str(payload.get("client_uri") or ""),
        )
        return JsonResponse(
            {
                "client_id": client.client_id,
                "client_name": client.client_name,
                "redirect_uris": client.redirect_uris,
                "token_endpoint_auth_method": "none",
                "grant_types": SUPPORTED_GRANT_TYPES,
                "response_types": SUPPORTED_RESPONSE_TYPES,
                "client_id_issued_at": int(client.created_at.timestamp()),
            },
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class Authorize(View):
    """
    The student-facing half: sign in, see what is being asked for, approve.

    GET renders the form. POST checks the password and issues the code. The
    request parameters ride along in hidden fields rather than a session, so
    this stays stateless and works with the DatabaseCache setup.
    """

    template = "mcp_server/authorize.html"

    def get(self, request):
        context = self._validate(request.GET)
        if isinstance(context, HttpResponse):
            return context
        return render(request, self.template, context)

    def post(self, request):
        context = self._validate(request.POST)
        if isinstance(context, HttpResponse):
            return context

        if request.POST.get("action") == "deny":
            return _redirect_with_error(
                context["redirect_uri"],
                "access_denied",
                "The student declined the request.",
                context["state"],
            )

        email = (request.POST.get("email") or "").strip()
        password = request.POST.get("password") or ""
        user = authenticate(request, username=email, password=password)
        if user is None or not user.is_active:
            return render(
                request,
                self.template,
                {**context, "error": "Invalid email or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        student = Student.objects.filter(user=user, is_active=True).first()
        if student is None:
            return render(
                request,
                self.template,
                {**context, "error": "This account cannot book laundry."},
                status=status.HTTP_403_FORBIDDEN,
            )

        _, code = OAuthAuthorizationCode.issue(
            client=context["client"],
            student=student,
            redirect_uri=context["redirect_uri"],
            code_challenge=context["code_challenge"],
        )
        params = {"code": code}
        if context["state"]:
            params["state"] = context["state"]
        joiner = "&" if urlparse(context["redirect_uri"]).query else "?"
        return HttpResponseRedirect(
            f"{context['redirect_uri']}{joiner}{urlencode(params)}"
        )

    def _validate(self, data):
        """
        Returns a template context dict, or a finished HttpResponse.

        client_id and redirect_uri failures are rendered, never redirected —
        redirecting to an unvalidated URI is an open redirect.
        """
        client_id = data.get("client_id")
        redirect_uri = data.get("redirect_uri")
        state = data.get("state") or ""

        client = OAuthClient.objects.filter(client_id=client_id, is_active=True).first()
        if client is None:
            return _oauth_error("invalid_client", "Unknown client_id.")
        if not redirect_uri or not client.allows_redirect(redirect_uri):
            return _oauth_error(
                "invalid_redirect_uri",
                "redirect_uri does not exactly match a registered URI.",
            )

        # Past this point the redirect target is trusted, so errors can ride back
        # to the client where it can show them.
        if data.get("response_type") != "code":
            return _redirect_with_error(
                redirect_uri, "unsupported_response_type", "Only 'code' is supported.", state
            )
        if data.get("code_challenge_method") not in SUPPORTED_CODE_CHALLENGE_METHODS:
            return _redirect_with_error(
                redirect_uri,
                "invalid_request",
                "code_challenge_method must be S256.",
                state,
            )
        code_challenge = data.get("code_challenge") or ""
        if not code_challenge:
            return _redirect_with_error(
                redirect_uri, "invalid_request", "code_challenge is required.", state
            )

        return {
            "client": client,
            "client_id": client_id,
            "client_name": client.client_name,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": MCP_SCOPE,
            "error": None,
        }


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


@method_decorator(csrf_exempt, name="dispatch")
class Token(View):
    """Exchange a code (or a refresh token) for an MCP access token."""

    def post(self, request):
        grant_type = request.POST.get("grant_type")
        if grant_type == "authorization_code":
            return self._authorization_code(request)
        if grant_type == "refresh_token":
            return self._refresh(request)
        return _oauth_error(
            "unsupported_grant_type", f"Unsupported grant_type {grant_type!r}."
        )

    def _authorization_code(self, request):
        code = request.POST.get("code")
        verifier = request.POST.get("code_verifier")
        client_id = request.POST.get("client_id")
        redirect_uri = request.POST.get("redirect_uri")

        row = OAuthAuthorizationCode.resolve(code)
        if row is None:
            return _oauth_error("invalid_grant", "Unknown authorization code.")

        if not row.is_usable:
            # A consumed code being presented again means it leaked. The client
            # that legitimately redeemed it already has tokens; cut them off.
            if row.consumed_at is not None:
                revoked = OAuthRefreshToken.objects.filter(
                    client=row.client, student=row.student, revoked_at__isnull=True
                )
                for token in revoked:
                    token.revoke_chain()
                McpToken.objects.filter(
                    student=row.student, oauth_client=row.client, revoked_at__isnull=True
                ).update(revoked_at=row.consumed_at)
                logger.warning(
                    "Replayed OAuth code for client=%s student=%s; grant revoked.",
                    row.client_id,
                    row.student_id,
                )
            return _oauth_error("invalid_grant", "Authorization code is no longer valid.")

        if row.client.client_id != client_id:
            return _oauth_error("invalid_grant", "Code was issued to another client.")
        if redirect_uri != row.redirect_uri:
            return _oauth_error("invalid_grant", "redirect_uri does not match the code.")
        if not verify_pkce(verifier, row.code_challenge):
            return _oauth_error("invalid_grant", "PKCE verification failed.")

        with transaction.atomic():
            row.consume()
            return self._issue(row.client, row.student, row.scope)

    def _refresh(self, request):
        presented = request.POST.get("refresh_token")
        client_id = request.POST.get("client_id")

        row = OAuthRefreshToken.resolve(presented)
        if row is None:
            return _oauth_error("invalid_grant", "Unknown refresh token.")
        if row.client.client_id != client_id:
            return _oauth_error("invalid_grant", "Token was issued to another client.")

        if not row.is_usable:
            # Rotation means a revoked token being replayed is either theft or a
            # broken client. Either way, cut the whole chain — and the access
            # tokens issued alongside it, which would otherwise stay valid for
            # up to an hour after we already decided the grant was compromised.
            revoked = row.revoke_chain()
            access_revoked = McpToken.objects.filter(
                student=row.student, oauth_client=row.client, revoked_at__isnull=True
            ).update(revoked_at=timezone.now())
            logger.warning(
                "Replayed OAuth refresh token for client=%s; revoked %d refresh "
                "and %d access token(s).",
                row.client_id,
                revoked,
                access_revoked,
            )
            return _oauth_error("invalid_grant", "Refresh token is no longer valid.")

        with transaction.atomic():
            row.revoke()
            return self._issue(row.client, row.student, row.scope, rotated_from=row)

    @staticmethod
    def _issue(client, student, scope, rotated_from=None):
        _, access = McpToken.issue_for_oauth(student=student, client=client, scope=scope)
        _, refresh = OAuthRefreshToken.issue(
            client=client, student=student, scope=scope, rotated_from=rotated_from
        )
        response = JsonResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "refresh_token": refresh,
                "scope": scope,
            }
        )
        # Credentials must never be cached by an intermediary.
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response
