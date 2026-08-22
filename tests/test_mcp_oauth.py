"""
The OAuth 2.1 flow that hosted connectors (claude.ai, ChatGPT) use.

These tests walk the whole dance a connector performs — discover, register,
authorize, exchange, call, refresh — and then attack it. The attacks are the
point: an authorization server that only works on the happy path is not an
authorization server.
"""

import base64
import hashlib
import json
import secrets
from datetime import time, timedelta
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status

from laundry.models import (
    Booking,
    BookingChannel,
    Gender,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    MachineKind,
    Student,
)
from mcp_server.models import (
    McpToken,
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthRefreshToken,
)
from mcp_server.oauth_views import verify_pkce

User = get_user_model()

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


class OAuthWorldMixin:
    def make_world(self):
        self.institute = Institute.objects.create(
            name="GIM Test", allowed_email_domains=["gim.ac.in"]
        )
        InstituteRule.objects.create(
            institute=self.institute,
            quota_limit=10,
            quota_window_days=7,
            cooldown_hours=0,
            advance_window_days=7,
            cancellation_cutoff_hours=6,
            dryer_cap_enabled=False,
        )
        self.hostel = Hostel.objects.create(
            institute=self.institute, name="Boys 1", gender=Gender.MALE
        )
        self.washer = Machine.objects.create(
            hostel=self.hostel,
            kind=MachineKind.WASHER,
            location_name="3rd Floor · A Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.password = "LundriiTest9!"
        self.user = User.objects.create_user(
            email="aarav@gim.ac.in", password=self.password
        )
        self.student = Student.objects.create(
            user=self.user,
            institute=self.institute,
            name="Aarav Mehta",
            gender=Gender.MALE,
            home_hostel=self.hostel,
            email_verified_at=timezone.now(),
        )
        self.client_row = OAuthClient.register(
            client_name="Claude", redirect_uris=[REDIRECT_URI]
        )
        self.verifier, self.challenge = make_pkce()
        self.tomorrow = timezone.localdate() + timedelta(days=1)

    def authorize_params(self, **overrides):
        params = {
            "response_type": "code",
            "client_id": self.client_row.client_id,
            "redirect_uri": REDIRECT_URI,
            "state": "xyz-state",
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
        }
        params.update(overrides)
        return params

    def approve(self, **overrides):
        """Run the consent form and return the authorization code."""
        payload = {
            **self.authorize_params(**overrides),
            "email": self.user.email,
            "password": self.password,
            "action": "approve",
        }
        response = self.client.post("/oauth/authorize", payload)
        assert response.status_code == 302, response.content
        return parse_qs(urlparse(response["Location"]).query)["code"][0]

    def exchange(self, code, **overrides):
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_row.client_id,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": self.verifier,
        }
        payload.update(overrides)
        return self.client.post("/oauth/token", payload)

    def mcp(self, access_token, method="tools/list", params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params:
            body["params"] = params
        return self.client.post(
            "/mcp/",
            data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )


class PkceTests(TestCase):
    def test_matching_verifier(self):
        verifier, challenge = make_pkce()
        self.assertTrue(verify_pkce(verifier, challenge))

    def test_mismatched_verifier(self):
        _, challenge = make_pkce()
        other, _ = make_pkce()
        self.assertFalse(verify_pkce(other, challenge))

    def test_empty_inputs_never_pass(self):
        _, challenge = make_pkce()
        self.assertFalse(verify_pkce("", challenge))
        self.assertFalse(verify_pkce("anything", ""))

    def test_challenge_is_unpadded_base64url(self):
        _, challenge = make_pkce()
        self.assertNotIn("=", challenge)
        self.assertNotIn("+", challenge)
        self.assertNotIn("/", challenge)


class DiscoveryTests(OAuthWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_protected_resource_metadata(self):
        response = self.client.get("/.well-known/oauth-protected-resource")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body["resource"].endswith("/mcp/"))
        self.assertEqual(len(body["authorization_servers"]), 1)

    def test_authorization_server_metadata(self):
        body = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertTrue(body["authorization_endpoint"].endswith("/oauth/authorize"))
        self.assertTrue(body["token_endpoint"].endswith("/oauth/token"))
        self.assertTrue(body["registration_endpoint"].endswith("/oauth/register"))
        self.assertEqual(body["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(body["grant_types_supported"], ["authorization_code", "refresh_token"])

    def test_plain_pkce_is_not_advertised(self):
        """OAuth 2.1 drops `plain`; advertising it would invite downgrades."""
        body = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertNotIn("plain", body["code_challenge_methods_supported"])

    def test_unauthorised_mcp_call_points_at_the_metadata(self):
        """This header is how a hosted connector discovers where to log in."""
        response = self.client.post(
            "/mcp/", data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn(
            "/.well-known/oauth-protected-resource",
            response.headers["WWW-Authenticate"],
        )


class ClientRegistrationTests(OAuthWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def register(self, payload):
        return self.client.post(
            "/oauth/register", data=json.dumps(payload), content_type="application/json"
        )

    def test_registration_issues_a_client_id_and_no_secret(self):
        response = self.register(
            {"client_name": "ChatGPT", "redirect_uris": ["https://chatgpt.com/cb"]}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertTrue(body["client_id"])
        self.assertNotIn("client_secret", body)
        self.assertEqual(body["token_endpoint_auth_method"], "none")

    def test_redirect_uris_are_required(self):
        self.assertEqual(self.register({"client_name": "X"}).status_code, 400)
        self.assertEqual(
            self.register({"client_name": "X", "redirect_uris": []}).status_code, 400
        )

    def test_remote_http_redirect_is_refused(self):
        """An http:// callback would carry the authorization code in clear."""
        response = self.register(
            {"client_name": "X", "redirect_uris": ["http://evil.example.com/cb"]}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["error"], "invalid_redirect_uri")

    def test_localhost_http_is_allowed_for_native_clients(self):
        response = self.register(
            {"client_name": "CLI", "redirect_uris": ["http://127.0.0.1:33418/cb"]}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_malformed_body(self):
        response = self.client.post(
            "/oauth/register", data="not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AuthorizeTests(OAuthWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_consent_screen_names_the_client(self):
        response = self.client.get("/oauth/authorize", self.authorize_params())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, "Claude")
        self.assertContains(response, "book laundry on your behalf")

    def test_unknown_client_is_rendered_not_redirected(self):
        """Redirecting on an unvalidated client would be an open redirect."""
        response = self.client.get(
            "/oauth/authorize", self.authorize_params(client_id="lcli_nope")
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["error"], "invalid_client")

    def test_unregistered_redirect_uri_is_rendered_not_redirected(self):
        response = self.client.get(
            "/oauth/authorize",
            self.authorize_params(redirect_uri="https://evil.example.com/cb"),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["error"], "invalid_redirect_uri")

    def test_redirect_uri_must_match_exactly_not_by_prefix(self):
        """Prefix matching is how codes get delivered to an attacker's path."""
        response = self.client.get(
            "/oauth/authorize",
            self.authorize_params(redirect_uri=REDIRECT_URI + "/../evil"),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_pkce_is_refused(self):
        response = self.client.get(
            "/oauth/authorize", self.authorize_params(code_challenge="")
        )
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response["Location"]).query)
        self.assertEqual(query["error"], ["invalid_request"])

    def test_plain_pkce_is_refused(self):
        response = self.client.get(
            "/oauth/authorize", self.authorize_params(code_challenge_method="plain")
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            parse_qs(urlparse(response["Location"]).query)["error"],
            ["invalid_request"],
        )

    def test_wrong_password_re_renders_the_form(self):
        response = self.client.post(
            "/oauth/authorize",
            {
                **self.authorize_params(),
                "email": self.user.email,
                "password": "wrong",
                "action": "approve",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertContains(
            response, "Invalid email or password", status_code=401
        )
        self.assertEqual(OAuthAuthorizationCode.objects.count(), 0)

    def test_denying_redirects_with_access_denied(self):
        response = self.client.post(
            "/oauth/authorize",
            {
                **self.authorize_params(),
                "email": self.user.email,
                "password": self.password,
                "action": "deny",
            },
        )
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response["Location"]).query)
        self.assertEqual(query["error"], ["access_denied"])
        self.assertEqual(query["state"], ["xyz-state"])
        self.assertEqual(OAuthAuthorizationCode.objects.count(), 0)

    def test_approval_returns_a_code_and_preserves_state(self):
        response = self.client.post(
            "/oauth/authorize",
            {
                **self.authorize_params(),
                "email": self.user.email,
                "password": self.password,
                "action": "approve",
            },
        )
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response["Location"]).query)
        self.assertTrue(query["code"][0].startswith("lcod_"))
        self.assertEqual(query["state"], ["xyz-state"])

    def test_code_is_stored_hashed(self):
        code = self.approve()
        row = OAuthAuthorizationCode.objects.get()
        self.assertNotEqual(row.code_hash, code)
        self.assertEqual(OAuthAuthorizationCode.resolve(code), row)


class TokenExchangeTests(OAuthWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_full_flow_yields_a_working_mcp_token(self):
        code = self.approve()
        response = self.exchange(code)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["token_type"], "Bearer")
        self.assertEqual(body["expires_in"], 3600)
        self.assertTrue(body["access_token"].startswith("lmcp_"))
        self.assertTrue(body["refresh_token"].startswith("lref_"))

        listing = self.mcp(body["access_token"])
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(len(listing.json()["result"]["tools"]), 4)

    def test_token_response_is_not_cacheable(self):
        response = self.exchange(self.approve())
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_access_token_is_short_lived_and_tied_to_the_client(self):
        self.exchange(self.approve())
        token = McpToken.objects.get()
        self.assertTrue(token.is_oauth)
        self.assertEqual(token.oauth_client, self.client_row)
        self.assertIsNotNone(token.expires_at)
        self.assertLessEqual(
            token.expires_at, timezone.now() + timedelta(seconds=3601)
        )

    def test_wrong_verifier_is_refused(self):
        other_verifier, _ = make_pkce()
        response = self.exchange(self.approve(), code_verifier=other_verifier)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertEqual(McpToken.objects.count(), 0)

    def test_missing_verifier_is_refused(self):
        response = self.exchange(self.approve(), code_verifier="")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_code_from_another_client_is_refused(self):
        other = OAuthClient.register(
            client_name="Impostor", redirect_uris=[REDIRECT_URI]
        )
        response = self.exchange(self.approve(), client_id=other.client_id)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(McpToken.objects.count(), 0)

    def test_mismatched_redirect_uri_is_refused(self):
        response = self.exchange(
            self.approve(), redirect_uri="https://claude.ai/other"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_code_is_refused(self):
        self.assertEqual(self.exchange("lcod_nope").status_code, 400)

    def test_expired_code_is_refused(self):
        code = self.approve()
        row = OAuthAuthorizationCode.objects.get()
        row.expires_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["expires_at"])
        self.assertEqual(self.exchange(code).status_code, 400)
        self.assertEqual(McpToken.objects.count(), 0)

    def test_replaying_a_code_revokes_the_whole_grant(self):
        """
        A code used twice means it leaked. The client that redeemed it first
        already holds tokens, and we cannot tell which holder is the attacker,
        so both are cut off.
        """
        code = self.approve()
        first = self.exchange(code)
        access = first.json()["access_token"]
        self.assertEqual(self.mcp(access).status_code, status.HTTP_200_OK)

        replay = self.exchange(code)
        self.assertEqual(replay.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(replay.json()["error"], "invalid_grant")

        # The originally-issued token is now dead.
        self.assertEqual(self.mcp(access).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(
            OAuthRefreshToken.objects.filter(revoked_at__isnull=True).exists()
        )

    def test_unsupported_grant_type(self):
        response = self.client.post("/oauth/token", {"grant_type": "password"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["error"], "unsupported_grant_type")


class RefreshTokenTests(OAuthWorldMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.tokens = self.exchange(self.approve()).json()

    def refresh(self, token, **overrides):
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_id": self.client_row.client_id,
        }
        payload.update(overrides)
        return self.client.post("/oauth/token", payload)

    def test_refresh_issues_a_new_pair(self):
        response = self.refresh(self.tokens["refresh_token"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertNotEqual(body["access_token"], self.tokens["access_token"])
        self.assertNotEqual(body["refresh_token"], self.tokens["refresh_token"])
        self.assertEqual(self.mcp(body["access_token"]).status_code, 200)

    def test_refresh_tokens_rotate(self):
        """The old refresh token stops working the moment it is redeemed."""
        self.refresh(self.tokens["refresh_token"])
        again = self.refresh(self.tokens["refresh_token"])
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)

    def test_replaying_a_rotated_token_revokes_the_chain(self):
        second = self.refresh(self.tokens["refresh_token"]).json()
        third = self.refresh(second["refresh_token"]).json()

        # Replay the first, long-rotated token.
        replay = self.refresh(self.tokens["refresh_token"])
        self.assertEqual(replay.status_code, status.HTTP_400_BAD_REQUEST)

        # Every descendant is dead, so an attacker holding any of them is out.
        self.assertEqual(
            self.refresh(third["refresh_token"]).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertFalse(
            OAuthRefreshToken.objects.filter(revoked_at__isnull=True).exists()
        )

    def test_chain_revocation_also_kills_live_access_tokens(self):
        """
        Cutting the refresh chain is pointless if the access token issued with
        it keeps working for the rest of its hour.
        """
        second = self.refresh(self.tokens["refresh_token"]).json()
        self.assertEqual(self.mcp(second["access_token"]).status_code, 200)

        self.refresh(self.tokens["refresh_token"])  # replay -> compromise

        self.assertEqual(
            self.mcp(second["access_token"]).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.mcp(self.tokens["access_token"]).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_refresh_from_another_client_is_refused(self):
        other = OAuthClient.register(client_name="Impostor", redirect_uris=[REDIRECT_URI])
        response = self.refresh(
            self.tokens["refresh_token"], client_id=other.client_id
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_refresh_token(self):
        self.assertEqual(self.refresh("lref_nope").status_code, 400)


class OAuthAccessScopeTests(OAuthWorldMixin, TestCase):
    """What an OAuth-issued token can and cannot do."""

    def setUp(self):
        self.make_world()
        self.access = self.exchange(self.approve()).json()["access_token"]

    def test_can_book_and_the_booking_is_recorded_as_mcp(self):
        response = self.mcp(
            self.access,
            "tools/call",
            {
                "name": "book_slot",
                "arguments": {
                    "machine_id": str(self.washer.id),
                    "date": self.tomorrow.isoformat(),
                    "hour": "14",
                },
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["result"]["isError"])
        self.assertEqual(Booking.objects.get().channel, BookingChannel.MCP)

    def test_acts_only_for_the_approving_student(self):
        other_user = User.objects.create_user(email="riya@gim.ac.in", password="x")
        other = Student.objects.create(
            user=other_user,
            institute=self.institute,
            name="Riya",
            gender=Gender.MALE,
            home_hostel=self.hostel,
            email_verified_at=timezone.now(),
        )
        starts = timezone.make_aware(
            timezone.datetime.combine(self.tomorrow, time(16, 0))
        )
        theirs = Booking.objects.create(
            student=other,
            machine=self.washer,
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
        )
        response = self.mcp(
            self.access, "tools/call", {"name": "list_my_bookings", "arguments": {}}
        )
        self.assertNotIn(str(theirs.id), response.json()["result"]["content"][0]["text"])

    def test_expired_access_token_is_refused(self):
        token = McpToken.objects.get()
        token.expires_at = timezone.now() - timedelta(seconds=1)
        token.save(update_fields=["expires_at"])
        self.assertEqual(self.mcp(self.access).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_personal_tokens_still_work_alongside_oauth(self):
        """Adding OAuth must not break the pasted-token path for CLI clients."""
        _, personal = McpToken.issue(student=self.student, name="Claude Code")
        self.assertEqual(self.mcp(personal).status_code, status.HTTP_200_OK)
        self.assertEqual(self.mcp(self.access).status_code, status.HTTP_200_OK)
