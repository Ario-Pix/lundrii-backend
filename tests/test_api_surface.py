"""
Whole-API guard rails.

The per-domain modules assert what each endpoint *does*. This module asserts
things that must hold across the API as a whole, so that adding a view without
permissions, dropping a route, or breaking the schema fails here rather than in
a client:

* every non-public route rejects anonymous callers;
* every route documented in SMOKE.md is still registered;
* the OpenAPI schema builds warning-free and still describes those routes — a
  drf-spectacular warning means an endpoint was silently dropped from the
  published contract, which nothing else in the suite would notice;
* the shared error and pagination envelopes keep their shape.
"""

import uuid

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from laundry.models import Hostel, Institute, Student

# Routes that are meant to be reachable without a token.
PUBLIC_PREFIXES = (
    "/health/",
    "/api/v1/auth/",
    # Guest Book browse — occupancy only; booking POST stays private.
    "/api/v1/hostels/",
    "/api/v1/machines/",
    "/api/schema",
    "/api/docs",
    "/admin/",  # Django's own admin, session-authenticated.
    # OAuth. Discovery metadata is public by specification — a client reads it
    # precisely because it has no credential yet — and /oauth/authorize is the
    # page where the student signs in. tests/test_mcp_oauth.py covers what each
    # of these does and does not hand out.
    "/.well-known/oauth-",
    "/oauth/",
)

SAMPLE_UUID = "00000000-0000-0000-0000-000000000000"


def _concrete_paths(patterns=None, prefix=""):
    """
    Walk the URLconf and yield a requestable path for every route.

    Path converters are filled with syntactically valid throwaway values; these
    requests are expected to be rejected before any lookup happens.
    """
    if patterns is None:
        patterns = get_resolver().url_patterns

    for entry in patterns:
        route = str(entry.pattern)
        if isinstance(entry, URLResolver):
            yield from _concrete_paths(entry.url_patterns, prefix + route)
        elif isinstance(entry, URLPattern):
            path = prefix + route
            if "(?P<" in path or path.endswith("$"):
                # Regex route — skip.
                continue
            if "drf_format_suffix" in path:
                # `.json`/`.api` variants of a route already covered above.
                continue
            for converter, sample in (
                ("uuid:", SAMPLE_UUID),
                ("int:", "1"),
                ("str:", "sample"),
                ("slug:", "sample"),
                ("", SAMPLE_UUID),
            ):
                while True:
                    start = path.find("<" + converter)
                    if start == -1:
                        break
                    end = path.find(">", start)
                    if end == -1:
                        break
                    path = path[:start] + sample + path[end + 1 :]
            if "<" in path or ">" in path:
                continue
            yield "/" + path.lstrip("/")


class RouteInventoryTests(SimpleTestCase):
    def test_urlconf_is_walkable(self):
        paths = list(_concrete_paths())
        self.assertGreater(len(paths), 40, "URLconf produced suspiciously few routes.")

    def test_documented_routes_are_registered(self):
        """Route names promised in SMOKE.md must still resolve."""
        expected = [
            # Auth
            "auth-register",
            "auth-signup-options",
            "auth-login",
            "auth-login-request-otp",
            "auth-login-verify-otp",
            "auth-logout",
            "auth-refresh",
            "auth-verify-email",
            "auth-resend-verification",
            "auth-forgot-password",
            "auth-reset-password",
            # Student
            "student-me",
            "student-me-hostels",
            "student-me-institute",
            "student-bookings",
            "student-tickets",
            "student-exchanges",
            "student-notifications",
            "student-notifications-read-all",
            "student-notification-preferences",
            "student-availability-misses",
            "student-mcp-tokens",
            "student-assistant-connections",
            # MCP connector endpoint (JSON-RPC, not under /api/v1).
            "mcp-endpoint",
            # Admin
            "admin-me",
            "admin-profile",
            "admin-suspensions",
            "admin-bookings-grid",
            "admin-bookings-export-csv",
            "admin-analytics-demand-by-hour",
            "admin-analytics-weekday-shape",
            "admin-analytics-channel-shares",
            "admin-audit-log",
            "admin-dashboard-summary",
            "admin-dashboard-attention",
            "admin-activity",
            "admin-change-password",
            # Schema
            "schema",
            "swagger-ui",
        ]
        for name in expected:
            with self.subTest(route=name):
                self.assertTrue(reverse(name))

    def test_parameterised_routes_are_registered(self):
        expected = {
            "student-hostel-machines": {"hostel_id": SAMPLE_UUID},
            "student-hostel-availability-now": {"hostel_id": SAMPLE_UUID},
            "student-machine-detail": {"machine_id": SAMPLE_UUID},
            "student-machine-slots": {"machine_id": SAMPLE_UUID},
            "student-booking-detail": {"booking_id": SAMPLE_UUID},
            "student-booking-cancel": {"booking_id": SAMPLE_UUID},
            "student-booking-move": {"booking_id": SAMPLE_UUID},
            "student-booking-move-options": {"booking_id": SAMPLE_UUID},
            "student-ticket-detail": {"ticket_id": SAMPLE_UUID},
            "student-exchange-approve": {"exchange_id": SAMPLE_UUID},
            "student-exchange-reject": {"exchange_id": SAMPLE_UUID},
            "student-exchange-withdraw": {"exchange_id": SAMPLE_UUID},
            "student-notification-read": {"notification_id": SAMPLE_UUID},
            "student-assistant-connection-disconnect": {"provider_id": "chatgpt"},
            "admin-booking-detail": {"booking_id": SAMPLE_UUID},
            "admin-booking-cancel": {"booking_id": SAMPLE_UUID},
            "admin-machine-offline-impact": {"machine_id": SAMPLE_UUID},
            "admin-machine-hours-impact": {"machine_id": SAMPLE_UUID},
        }
        for name, kwargs in expected.items():
            with self.subTest(route=name):
                self.assertTrue(reverse(name, kwargs=kwargs))

    def test_admin_crud_routers_are_registered(self):
        for basename in (
            "admin-institute",
            "admin-hostel",
            "admin-machine",
            "admin-rule",
            "admin-student",
            "admin-ticket",
        ):
            with self.subTest(router=basename):
                self.assertTrue(reverse(f"{basename}-list"))
                self.assertTrue(reverse(f"{basename}-detail", args=[SAMPLE_UUID]))

    def test_strike_router_exposes_only_revoke_and_delete(self):
        """Strikes are never listed — they are revoked by id."""
        self.assertTrue(reverse("admin-strike-detail", args=[SAMPLE_UUID]))
        self.assertTrue(reverse("admin-strike-revoke", args=[SAMPLE_UUID]))


class AnonymousAccessTests(APITestCase):
    """No API route may leak data to a caller without a token."""

    def test_every_private_route_rejects_anonymous_get(self):
        unprotected = []
        for path in _concrete_paths():
            if path.startswith(PUBLIC_PREFIXES):
                continue
            response = self.client.get(path)
            if response.status_code not in (
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
                status.HTTP_405_METHOD_NOT_ALLOWED,
            ):
                unprotected.append((path, response.status_code))
        self.assertEqual(unprotected, [], f"Routes reachable anonymously: {unprotected}")

    def test_anonymous_rejection_uses_the_standard_error_envelope(self):
        response = self.client.get("/api/v1/me")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["code"], "AUTHENTICATION_FAILED")
        self.assertIn("detail", response.data)

    def test_public_auth_routes_are_reachable_anonymously(self):
        for path in (
            "/api/v1/auth/login",
            "/api/v1/auth/register",
            "/api/v1/auth/forgot-password",
            "/api/v1/auth/verify-email",
        ):
            with self.subTest(path=path):
                response = self.client.post(path, {}, format="json")
                # Rejected for missing fields, not for missing credentials.
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data["code"], "VALIDATION_ERROR")

    def test_mcp_endpoint_rejects_anonymous_post(self):
        """
        The sweep above only issues GETs, and /mcp/ answers those with 405 —
        a pass for the wrong reason. POST is the method that carries tools, so
        assert that one directly.
        """
        response = self.client.post(
            "/mcp/",
            data='{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_garbage_token_is_rejected(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-jwt")
        response = client.get("/api/v1/me")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class ErrorEnvelopeTests(APITestCase):
    """Every error response carries a stable `code` plus a `detail` string."""

    password = "LundriiTest9!"

    def setUp(self):
        cache.clear()
        self.institute = Institute.objects.create(
            name="Goa Institute of Management",
            allowed_email_domains=["gim.ac.in"],
        )
        self.hostel = Hostel.objects.create(
            institute=self.institute, name="Ganga"
        )
        self.user = get_user_model().objects.create_user(
            email="aarav.mehta@gim.ac.in", password=self.password
        )
        Student.objects.create(
            user=self.user,
            institute=self.institute,
            home_hostel=self.hostel,
            name="Aarav Mehta",
            phone="+91 98220 41127",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.user).access_token}"
        )

    def tearDown(self):
        cache.clear()

    def test_unknown_object_maps_to_not_found(self):
        response = self.client.get(f"/api/v1/bookings/{uuid.uuid4()}")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "NOT_FOUND")
        self.assertIsInstance(response.data["detail"], str)

    def test_validation_failure_maps_to_validation_error(self):
        response = self.client.post("/api/v1/bookings", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "VALIDATION_ERROR")

    def test_student_is_denied_admin_routes(self):
        response = self.client.get("/api/v1/admin/institutes/")
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_401_UNAUTHORIZED),
        )
        self.assertIn(
            response.data["code"], ("PERMISSION_DENIED", "AUTHENTICATION_FAILED")
        )


class PaginationEnvelopeTests(APITestCase):
    """Paginated list responses keep the documented envelope."""

    password = "LundriiTest9!"

    def setUp(self):
        cache.clear()
        self.institute = Institute.objects.create(
            name="Goa Institute of Management",
            allowed_email_domains=["gim.ac.in"],
        )
        self.hostel = Hostel.objects.create(
            institute=self.institute, name="Ganga"
        )
        self.user = get_user_model().objects.create_user(
            email="aarav.mehta@gim.ac.in", password=self.password
        )
        Student.objects.create(
            user=self.user,
            institute=self.institute,
            home_hostel=self.hostel,
            name="Aarav Mehta",
            phone="+91 98220 41127",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.user).access_token}"
        )

    def tearDown(self):
        cache.clear()

    def test_notifications_list_envelope(self):
        response = self.client.get("/api/v1/notifications")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for key in ("count", "next", "previous", "page", "page_size", "results"):
            self.assertIn(key, response.data)
        self.assertEqual(response.data["page"], 1)
        self.assertEqual(response.data["page_size"], 20)
        self.assertIsInstance(response.data["results"], list)

    def test_page_size_query_param_is_honoured(self):
        response = self.client.get("/api/v1/notifications?page_size=5")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["page_size"], 5)

    def test_page_size_is_capped(self):
        response = self.client.get("/api/v1/notifications?page_size=5000")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertLessEqual(response.data["page_size"], 100)


class OpenApiSchemaTests(TestCase):
    """The published contract must keep building and keep describing the API."""

    @staticmethod
    def _build_schema():
        """
        Generate the schema, returning it alongside anything drf-spectacular
        complained about while building it.

        A warning here is not cosmetic: drf-spectacular drops an endpoint it
        cannot introspect, so the published contract silently loses routes.
        """
        from drf_spectacular.drainage import GENERATOR_STATS
        from drf_spectacular.generators import SchemaGenerator

        GENERATOR_STATS.reset()
        schema = SchemaGenerator().get_schema(request=None, public=True)
        problems = sorted(GENERATOR_STATS._warn_cache) + sorted(
            GENERATOR_STATS._error_cache
        )
        return schema, problems

    def test_schema_builds_without_warnings_or_errors(self):
        schema, problems = self._build_schema()
        self.assertEqual(
            problems,
            [],
            "drf-spectacular reported schema problems; endpoints it cannot "
            "introspect are dropped from /api/schema/:\n  "
            + "\n  ".join(problems),
        )
        self.assertEqual(schema["info"]["title"], "Lundrii API")
        self.assertIn("paths", schema)
        self.assertGreater(len(schema["paths"]), 30)

    def test_schema_describes_the_core_paths(self):
        paths = self._build_schema()[0]["paths"]
        for path in (
            "/api/v1/auth/register",
            "/api/v1/auth/login",
            "/api/v1/me",
            "/api/v1/me/assistant-connections",
            "/api/v1/me/assistant-connections/{provider_id}",
            "/api/v1/bookings",
            "/api/v1/tickets",
            "/api/v1/exchanges",
            "/api/v1/notifications",
            "/api/v1/admin/institutes/",
            "/api/v1/admin/machines/",
            # Both of these were missing from the schema until their responses
            # were declared explicitly — a plain APIView drf-spectacular cannot
            # introspect is dropped without failing anything.
            "/api/v1/admin/bookings/{booking_id}/cancel/",
            "/api/v1/admin/bookings/export.csv",
        ):
            with self.subTest(path=path):
                self.assertIn(path, paths)

    def test_csv_export_is_documented_as_csv(self):
        paths = self._build_schema()[0]["paths"]
        operation = paths["/api/v1/admin/bookings/export.csv"]["get"]
        self.assertEqual(
            list(operation["responses"]["200"]["content"]), ["text/csv"]
        )

    def test_admin_cancel_takes_no_request_body(self):
        paths = self._build_schema()[0]["paths"]
        operation = paths["/api/v1/admin/bookings/{booking_id}/cancel/"]["post"]
        self.assertNotIn("requestBody", operation)
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/BookingDetail"},
        )

    def test_the_two_strike_serializers_get_distinct_components(self):
        """
        Student and admin both expose a `StrikeSerializer`. Sharing one
        component name would let whichever is generated last define both.
        """
        components = self._build_schema()[0]["components"]["schemas"]
        self.assertIn("Strike", components)
        self.assertIn("AdminStrike", components)
        self.assertNotEqual(components["Strike"], components["AdminStrike"])

    def test_status_enums_have_stable_names(self):
        """
        Two different `status` choice sets collide on name. Left alone,
        drf-spectacular invents a suffix that moves as the schema is
        reshuffled, so generated clients churn between builds.
        """
        components = self._build_schema()[0]["components"]["schemas"]
        self.assertIn("ExchangeStatusEnum", components)
        self.assertIn("TicketStatusEnum", components)
        self.assertEqual(
            set(components["TicketStatusEnum"]["enum"]), {"open", "resolved"}
        )
        self.assertEqual(
            set(components["ExchangeStatusEnum"]["enum"]),
            {"pending", "approved", "rejected", "expired", "failed"},
        )

    def test_schema_endpoint_serves(self):
        response = self.client.get("/api/schema/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_docs_endpoint_serves(self):
        response = self.client.get("/api/docs/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
