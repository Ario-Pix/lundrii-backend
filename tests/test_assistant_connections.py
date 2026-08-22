"""
Profile's ChatGPT / Claude connection status.

Connected is a live OAuth refresh grant classified by the client's redirect
host, not a 1-hour access McpToken and not a personal paste token.
"""

from datetime import timedelta

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from laundry.models import Gender, Student
from mcp_server.models import McpToken, OAuthClient, OAuthRefreshToken
from mcp_server.providers import classify_oauth_client
from tests.test_mcp_oauth import OAuthWorldMixin


class ClassifyOAuthClientTests(TestCase):
    def test_claude_callback_uri(self):
        client = OAuthClient.register(
            client_name="Anything",
            redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        )
        self.assertEqual(classify_oauth_client(client), "claude")

    def test_chatgpt_like_uri(self):
        client = OAuthClient.register(
            client_name="Connector",
            redirect_uris=["https://chatgpt.com/connector/oauth/callback"],
        )
        self.assertEqual(classify_oauth_client(client), "chatgpt")

    def test_openai_host(self):
        client = OAuthClient.register(
            client_name="GPT",
            redirect_uris=["https://auth.openai.com/mcp/callback"],
        )
        self.assertEqual(classify_oauth_client(client), "chatgpt")

    def test_anthropic_host(self):
        client = OAuthClient.register(
            client_name="Claude",
            redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
            client_uri="https://www.anthropic.com",
        )
        self.assertEqual(classify_oauth_client(client), "claude")

    def test_unknown_redirect_host(self):
        client = OAuthClient.register(
            client_name="Mystery",
            redirect_uris=["https://example.com/oauth/callback"],
        )
        self.assertEqual(classify_oauth_client(client), "unknown")

    def test_conflicting_hosts_stay_unknown(self):
        client = OAuthClient.register(
            client_name="Both",
            redirect_uris=[
                "https://chatgpt.com/cb",
                "https://claude.ai/cb",
            ],
        )
        self.assertEqual(classify_oauth_client(client), "unknown")

    def test_name_hint_when_hosts_are_empty(self):
        client = OAuthClient.register(client_name="ChatGPT Desktop", redirect_uris=[])
        self.assertEqual(classify_oauth_client(client), "chatgpt")


class AssistantConnectionApiTests(OAuthWorldMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.api = APIClient()
        self.api.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {RefreshToken.for_user(self.student.user).access_token}"
            )
        )

    def _provider(self, body, provider_id):
        return next(p for p in body["providers"] if p["id"] == provider_id)

    def test_disconnected_by_default(self):
        response = self.api.get("/api/v1/me/assistant-connections")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.data
        self.assertTrue(body["mcpUrl"].endswith("/mcp/"))
        ids = [p["id"] for p in body["providers"]]
        self.assertEqual(ids, ["chatgpt", "claude"])
        for row in body["providers"]:
            self.assertEqual(row["status"], "disconnected")
            self.assertIsNone(row["connectedAt"])
            self.assertTrue(row["openUrl"])
            self.assertGreaterEqual(len(row["steps"]), 3)

    def test_claude_oauth_grant_shows_connected_after_access_expires(self):
        tokens = self.exchange(self.approve()).json()
        access_row = McpToken.objects.get()
        access_row.expires_at = timezone.now() - timedelta(minutes=1)
        access_row.save(update_fields=["expires_at"])

        self.assertEqual(
            self.mcp(tokens["access_token"]).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

        body = self.api.get("/api/v1/me/assistant-connections").data
        self.assertEqual(self._provider(body, "claude")["status"], "connected")
        self.assertIsNotNone(self._provider(body, "claude")["connectedAt"])
        self.assertEqual(self._provider(body, "chatgpt")["status"], "disconnected")

    def test_unknown_redirect_host_does_not_mark_known_providers(self):
        mystery = OAuthClient.register(
            client_name="Other",
            redirect_uris=["https://example.com/oauth/callback"],
        )
        OAuthRefreshToken.issue(client=mystery, student=self.student)
        body = self.api.get("/api/v1/me/assistant-connections").data
        self.assertEqual(self._provider(body, "chatgpt")["status"], "disconnected")
        self.assertEqual(self._provider(body, "claude")["status"], "disconnected")
        unknown = self._provider(body, "unknown")
        self.assertEqual(unknown["status"], "connected")
        self.assertEqual(unknown["label"], "Other assistant")

    def test_chatgpt_grant_is_isolated_from_another_student(self):
        gpt = OAuthClient.register(
            client_name="ChatGPT",
            redirect_uris=["https://chatgpt.com/connector/oauth"],
        )
        OAuthRefreshToken.issue(client=gpt, student=self.student)

        other_user = self.student.user.__class__.objects.create_user(
            email="riya@gim.ac.in", password=self.password
        )
        other = Student.objects.create(
            user=other_user,
            institute=self.institute,
            name="Riya Sharma",
            gender=Gender.FEMALE,
            home_hostel=self.hostel,
            email_verified_at=timezone.now(),
        )
        other_api = APIClient()
        other_api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(other_user).access_token}"
        )

        mine = self.api.get("/api/v1/me/assistant-connections").data
        theirs = other_api.get("/api/v1/me/assistant-connections").data
        self.assertEqual(self._provider(mine, "chatgpt")["status"], "connected")
        self.assertEqual(self._provider(theirs, "chatgpt")["status"], "disconnected")

        response = other_api.delete("/api/v1/me/assistant-connections/chatgpt")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        mine_after = self.api.get("/api/v1/me/assistant-connections").data
        self.assertEqual(self._provider(mine_after, "chatgpt")["status"], "connected")
        self.assertFalse(other.oauth_refresh_tokens.exists())

    def test_disconnect_revokes_refresh_chain_and_access_token(self):
        tokens = self.exchange(self.approve()).json()
        self.assertEqual(self.mcp(tokens["access_token"]).status_code, status.HTTP_200_OK)

        _, personal = McpToken.issue(student=self.student, name="Claude Code")

        response = self.api.delete("/api/v1/me/assistant-connections/claude")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        body = self.api.get("/api/v1/me/assistant-connections").data
        self.assertEqual(self._provider(body, "claude")["status"], "disconnected")

        self.assertEqual(
            self.mcp(tokens["access_token"]).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        refresh = self.client.post(
            "/oauth/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": self.client_row.client_id,
            },
        )
        self.assertEqual(refresh.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNotNone(McpToken.resolve(personal))

    def test_unknown_provider_id_is_not_found(self):
        response = self.api.delete("/api/v1/me/assistant-connections/gemini")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "NOT_FOUND")

    def test_anonymous_is_rejected(self):
        anon = APIClient()
        self.assertEqual(
            anon.get("/api/v1/me/assistant-connections").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_routes_are_registered(self):
        self.assertTrue(reverse("student-assistant-connections"))
        self.assertTrue(
            reverse(
                "student-assistant-connection-disconnect",
                kwargs={"provider_id": "chatgpt"},
            )
        )

    @override_settings(MCP_PUBLIC_URL="https://api.lundrii.app")
    def test_mcp_url_honours_public_override(self):
        body = self.api.get("/api/v1/me/assistant-connections").data
        self.assertEqual(body["mcpUrl"], "https://api.lundrii.app/mcp/")


class ClassifyOAuthClientNoDbTests(SimpleTestCase):
    def test_none_is_unknown(self):
        self.assertEqual(classify_oauth_client(None), "unknown")
