"""
Booking source detection.

`Booking.channel` has to fill itself in — no client passes it, and no request
body carries it. These tests pin where the value comes from and, just as
importantly, that a client cannot be recorded as something it is not on the one
path where the answer is certain (MCP).
"""

from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from authentication.services.tokens import issue_jwt_pair
from base.clients import (
    CHANNEL_ANDROID,
    CHANNEL_APP,
    CHANNEL_IOS,
    CHANNEL_MCP,
    CHANNEL_WEBSITE,
    channel_from_user_agent,
    normalise_platform,
)
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
from laundry.services.analytics import channel_display_name, channel_shares
from mcp_server.models import McpToken

User = get_user_model()


class PlatformParsingTests(SimpleTestCase):
    def test_explicit_platform_names(self):
        for raw, expected in (
            ("ios", CHANNEL_IOS),
            ("iOS", CHANNEL_IOS),
            ("iPhone", CHANNEL_IOS),
            ("android", CHANNEL_ANDROID),
            ("  Android  ", CHANNEL_ANDROID),
            ("web", CHANNEL_WEBSITE),
            ("website", CHANNEL_WEBSITE),
            ("mcp", CHANNEL_MCP),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalise_platform(raw), expected)

    def test_unknown_platform_is_not_invented(self):
        for raw in ("", None, "windows-phone", "smart-fridge"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalise_platform(raw))

    def test_user_agent_sniffing(self):
        for agent, expected in (
            ("Lundrii-Android/1.2", CHANNEL_ANDROID),
            ("Lundrii-iOS/1.2", CHANNEL_IOS),
            ("Dalvik/2.1.0 (Linux; U; Android 14; Pixel 8)", CHANNEL_ANDROID),
            ("Lundrii/1.0 CFNetwork/1494 Darwin/23.4.0", CHANNEL_IOS),
            (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) Safari/604.1",
                CHANNEL_IOS,
            ),
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/124.0",
                CHANNEL_WEBSITE,
            ),
        ):
            with self.subTest(agent=agent):
                self.assertEqual(channel_from_user_agent(agent), expected)

    def test_android_webview_is_android_not_website(self):
        """Android's WebView UA also says 'Mozilla'; order of checks matters."""
        agent = (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
        )
        self.assertEqual(channel_from_user_agent(agent), CHANNEL_ANDROID)

    def test_no_user_agent_is_not_a_guess(self):
        self.assertIsNone(channel_from_user_agent(None))
        self.assertIsNone(channel_from_user_agent(""))
        self.assertIsNone(channel_from_user_agent("curl/8.4.0"))


class ChannelWorldMixin:
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
            institute=self.institute, name="Boys 1"
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
        self.tomorrow = timezone.localdate() + timedelta(days=1)

    def book_via_api(self, access_token, hour, **headers):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}", **headers)
        return client.post(
            "/api/v1/bookings",
            {
                "items": [
                    {
                        "machineId": str(self.washer.id),
                        "date": self.tomorrow.isoformat(),
                        "hour": hour,
                    }
                ]
            },
            format="json",
        )


class JwtClientClaimTests(ChannelWorldMixin, TestCase):
    """Login stamps the client into the token; bookings read it back."""

    def setUp(self):
        self.make_world()

    def login(self, **headers):
        client = APIClient()
        response = client.post(
            "/api/v1/auth/login",
            {"email": self.user.email, "password": self.password},
            format="json",
            **headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_login_stamps_the_declared_platform(self):
        data = self.login(HTTP_X_CLIENT_PLATFORM="ios")
        self.assertEqual(AccessToken(data["access"])["client"], CHANNEL_IOS)

    def test_login_falls_back_to_user_agent(self):
        data = self.login(HTTP_USER_AGENT="Dalvik/2.1.0 (Linux; U; Android 14)")
        self.assertEqual(AccessToken(data["access"])["client"], CHANNEL_ANDROID)

    def test_explicit_header_beats_user_agent(self):
        data = self.login(
            HTTP_X_CLIENT_PLATFORM="ios",
            HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 14) Chrome/124.0",
        )
        self.assertEqual(AccessToken(data["access"])["client"], CHANNEL_IOS)

    def test_undeclared_client_gets_no_claim(self):
        data = self.login()
        self.assertNotIn("client", AccessToken(data["access"]))

    def test_claim_survives_a_token_refresh(self):
        """
        The client is stamped on the refresh token, so access tokens minted an
        hour later still know where the session came from.
        """
        data = self.login(HTTP_X_CLIENT_PLATFORM="ios")
        client = APIClient()
        refreshed = client.post(
            "/api/v1/auth/refresh", {"refresh": data["refresh"]}, format="json"
        )
        self.assertEqual(refreshed.status_code, status.HTTP_200_OK)
        self.assertEqual(AccessToken(refreshed.data["access"])["client"], CHANNEL_IOS)

    def test_booking_records_the_channel_from_the_token(self):
        for platform, expected in (
            ("ios", BookingChannel.IOS),
            ("android", BookingChannel.ANDROID),
            ("web", BookingChannel.WEBSITE),
        ):
            with self.subTest(platform=platform):
                Booking.objects.all().delete()
                data = self.login(HTTP_X_CLIENT_PLATFORM=platform)
                response = self.book_via_api(data["access"], 14)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(Booking.objects.get().channel, expected)

    def test_no_request_body_field_is_involved(self):
        """
        The API never accepts a channel from the caller. Sending one changes
        nothing — the token is the authority.
        """
        data = self.login(HTTP_X_CLIENT_PLATFORM="ios")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {data['access']}")
        response = client.post(
            "/api/v1/bookings",
            {
                "channel": "whatsapp",
                "items": [
                    {
                        "machineId": str(self.washer.id),
                        "date": self.tomorrow.isoformat(),
                        "hour": 14,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Booking.objects.get().channel, BookingChannel.IOS)

    def test_header_works_for_a_token_issued_before_the_claim_existed(self):
        """Older sessions have no claim; the per-request header still helps."""
        legacy = issue_jwt_pair(self.user)  # no client
        response = self.book_via_api(
            legacy["access"], 15, HTTP_X_CLIENT_PLATFORM="android"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Booking.objects.get().channel, BookingChannel.ANDROID)

    def test_unidentified_client_defaults_to_app(self):
        token = RefreshToken.for_user(self.user).access_token
        response = self.book_via_api(str(token), 16)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Booking.objects.get().channel, BookingChannel.APP)


class McpChannelTests(ChannelWorldMixin, TestCase):
    def setUp(self):
        self.make_world()
        _, self.mcp_token = McpToken.issue(student=self.student, name="Claude")

    def book_via_mcp(self, hour, **headers):
        import json

        return self.client.post(
            "/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "book_slot",
                        "arguments": {
                            "machine_id": str(self.washer.id),
                            "date": self.tomorrow.isoformat(),
                            "hour": str(hour),
                        },
                    },
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.mcp_token}",
            **headers,
        )

    def test_mcp_booking_is_recorded_as_mcp(self):
        response = self.book_via_mcp(14)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["result"]["isError"])
        self.assertEqual(Booking.objects.get().channel, BookingChannel.MCP)

    def test_mcp_cannot_be_disguised_as_an_app(self):
        """
        The credential decides, not the headers. An assistant claiming to be an
        iPhone is still recorded as MCP.
        """
        response = self.book_via_mcp(
            15,
            HTTP_X_CLIENT_PLATFORM="ios",
            HTTP_USER_AGENT="Lundrii-iOS/1.2",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Booking.objects.get().channel, BookingChannel.MCP)


class ChannelAnalyticsTests(ChannelWorldMixin, TestCase):
    """Admin analytics has to understand the new channel."""

    def setUp(self):
        self.make_world()

    def test_mcp_has_a_display_name(self):
        self.assertEqual(channel_display_name(BookingChannel.MCP), "Assistant (MCP)")

    def test_channel_shares_counts_mcp_bookings(self):
        starts = timezone.make_aware(
            timezone.datetime.combine(self.tomorrow, time(14, 0))
        )
        for offset, channel in enumerate(
            (BookingChannel.MCP, BookingChannel.MCP, BookingChannel.IOS)
        ):
            Booking.objects.create(
                student=self.student,
                machine=self.washer,
                starts_at=starts + timedelta(hours=offset),
                ends_at=starts + timedelta(hours=offset + 1),
                channel=channel,
            )
        shares = {row["name"]: row for row in channel_shares(institute_id=self.institute.id)}
        # 2 of 3 bookings are MCP.
        self.assertEqual(shares["Assistant (MCP)"]["pct"], 67)
        self.assertEqual(shares["iOS app"]["pct"], 33)

    def test_every_channel_display_name_is_reported(self):
        """
        channel_shares emits a hardcoded palette of display names. A channel
        whose name is missing from that palette is still counted in the total
        but never emitted, so those bookings vanish from the chart and every
        other percentage is understated. Adding a BookingChannel without adding
        its name to the palette must fail here.
        """
        reported = {row["name"] for row in channel_shares()}
        expected = {channel_display_name(value) for value in BookingChannel.values}
        self.assertEqual(
            expected - reported,
            set(),
            "these channels would be silently dropped from admin analytics",
        )

    def test_percentages_still_sum_to_100_with_mcp_in_the_mix(self):
        starts = timezone.make_aware(
            timezone.datetime.combine(self.tomorrow, time(0, 0))
        )
        for offset, channel in enumerate(BookingChannel.values):
            Booking.objects.create(
                student=self.student,
                machine=self.washer,
                starts_at=starts + timedelta(hours=offset),
                ends_at=starts + timedelta(hours=offset + 1),
                channel=channel,
            )
        rows = channel_shares(institute_id=self.institute.id)
        self.assertEqual(sum(row["pct"] for row in rows), 100)
