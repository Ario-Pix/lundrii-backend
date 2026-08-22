"""
The MCP connector: protocol, auth, and the booking tools.

The point of these tests is that a chat client gets exactly the same rules as
the mobile app. An assistant must not be able to book past a quota, into a
suspended account, or on another student's machine, and it must not be able to
reach anything at all without a valid connector token.
"""

import json
import uuid
from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from laundry.models import (
    Booking,
    Gender,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    MachineKind,
    Student,
)
from mcp_server import protocol
from mcp_server.models import TOKEN_PREFIX, McpToken

User = get_user_model()
MCP_URL = "/mcp/"


class McpWorldMixin:
    """A student with one washer and one dryer, and a working connector token."""

    def make_world(self, **rule_kwargs):
        self.institute = Institute.objects.create(
            name="GIM Test", allowed_email_domains=["gim.ac.in"]
        )
        defaults = dict(
            quota_limit=3,
            quota_window_days=7,
            cooldown_hours=0,
            advance_window_days=7,
            cancellation_cutoff_hours=6,
            dryer_cap_enabled=False,
        )
        defaults.update(rule_kwargs)
        self.rules = InstituteRule.objects.create(institute=self.institute, **defaults)
        self.boys = Hostel.objects.create(
            institute=self.institute, name="Boys 1", gender=Gender.MALE
        )
        self.girls = Hostel.objects.create(
            institute=self.institute, name="Girls 1", gender=Gender.FEMALE
        )
        self.washer = Machine.objects.create(
            hostel=self.boys,
            kind=MachineKind.WASHER,
            location_name="3rd Floor · A Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.dryer = Machine.objects.create(
            hostel=self.boys,
            kind=MachineKind.DRYER,
            location_name="Ground Floor · B Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.student = self.make_student("aarav@gim.ac.in", "Aarav Mehta", Gender.MALE)
        self.token_row, self.token = McpToken.issue(
            student=self.student, name="Claude desktop"
        )
        self.tomorrow = timezone.localdate() + timedelta(days=1)

    def make_student(self, email, name, gender, *, verified=True, suspended_until=None):
        user = User.objects.create_user(email=email, password="unused")
        return Student.objects.create(
            user=user,
            institute=self.institute,
            name=name,
            gender=gender,
            home_hostel=self.boys if gender == Gender.MALE else self.girls,
            email_verified_at=timezone.now() if verified else None,
            suspension_ends=suspended_until,
        )

    # -- MCP wire helpers --------------------------------------------------

    def rpc(self, method, params=None, *, token=None, request_id=1):
        body = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        return self._post(body, token=token)

    def notify(self, method, *, token=None):
        return self._post({"jsonrpc": "2.0", "method": method}, token=token)

    def _post(self, body, *, token=None):
        return self.client.post(
            MCP_URL,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token if token is None else token}",
        )

    def call_tool(self, name, arguments=None, *, token=None):
        """Call a tool and return (text, is_error)."""
        response = self.rpc(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            token=token,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = response.json()["result"]
        return result["content"][0]["text"], result.get("isError", False)


class ConnectorTokenModelTests(McpWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_plaintext_is_never_stored(self):
        self.assertTrue(self.token.startswith(TOKEN_PREFIX))
        self.assertNotEqual(self.token_row.token_hash, self.token)
        self.assertNotIn(self.token, self.token_row.token_hash)
        # The hint is a recogniser, not a usable fragment.
        self.assertLess(len(self.token_row.token_hint), len(self.token))

    def test_resolve_round_trip(self):
        self.assertEqual(McpToken.resolve(self.token), self.token_row)

    def test_resolve_rejects_unknown_and_malformed(self):
        self.assertIsNone(McpToken.resolve("lmcp_nope"))
        self.assertIsNone(McpToken.resolve("not-even-prefixed"))
        self.assertIsNone(McpToken.resolve(""))
        self.assertIsNone(McpToken.resolve(None))

    def test_revoked_token_stops_resolving(self):
        self.token_row.revoke()
        self.assertIsNone(McpToken.resolve(self.token))

    def test_expired_token_stops_resolving(self):
        self.token_row.expires_at = timezone.now() - timedelta(seconds=1)
        self.token_row.save(update_fields=["expires_at"])
        self.assertIsNone(McpToken.resolve(self.token))

    def test_token_dies_with_the_student(self):
        self.student.is_active = False
        self.student.save(update_fields=["is_active"])
        self.assertIsNone(McpToken.resolve(self.token))

    def test_token_dies_with_the_user_account(self):
        self.student.user.is_active = False
        self.student.user.save(update_fields=["is_active"])
        self.assertIsNone(McpToken.resolve(self.token))

    def test_tokens_are_unique_per_issue(self):
        _, second = McpToken.issue(student=self.student, name="ChatGPT")
        self.assertNotEqual(self.token, second)


class McpAuthenticationTests(McpWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_missing_token_is_rejected(self):
        response = self.client.post(
            MCP_URL, data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("WWW-Authenticate", response.headers)

    def test_invalid_token_is_rejected(self):
        response = self.rpc("tools/list", token="lmcp_totally-made-up")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_revoked_token_is_rejected(self):
        self.token_row.revoke()
        self.assertEqual(
            self.rpc("tools/list").status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_no_tool_runs_without_a_token(self):
        """Auth is checked before dispatch, so an unauthenticated call books nothing."""
        response = self.client.post(
            MCP_URL,
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
                            "hour": "14",
                        },
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(Booking.objects.count(), 0)

    def test_using_a_token_records_last_used(self):
        self.assertIsNone(self.token_row.last_used_at)
        self.rpc("ping")
        self.token_row.refresh_from_db()
        self.assertIsNotNone(self.token_row.last_used_at)

    def test_get_is_not_offered(self):
        response = self.client.get(MCP_URL)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(response.headers["Allow"], "POST")


class McpProtocolTests(McpWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_initialize_handshake(self):
        response = self.rpc(
            "initialize",
            {
                "protocolVersion": protocol.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "claude", "version": "1.0"},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = response.json()["result"]
        self.assertEqual(result["protocolVersion"], protocol.LATEST_PROTOCOL_VERSION)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], protocol.SERVER_NAME)
        self.assertIn("instructions", result)

    def test_initialize_echoes_a_supported_older_version(self):
        response = self.rpc("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(response.json()["result"]["protocolVersion"], "2024-11-05")

    def test_initialize_falls_back_for_an_unknown_version(self):
        response = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(
            response.json()["result"]["protocolVersion"],
            protocol.LATEST_PROTOCOL_VERSION,
        )

    def test_initialized_notification_gets_202_and_no_body(self):
        response = self.notify("notifications/initialized")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.content, b"")

    def test_ping(self):
        self.assertEqual(self.rpc("ping").json()["result"], {})

    def test_tools_list_advertises_every_tool(self):
        tools = self.rpc("tools/list").json()["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertEqual(
            names,
            {"find_available_slots", "book_slot", "list_my_bookings", "cancel_booking"},
        )
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["description"])
                self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_unknown_method_is_a_jsonrpc_error(self):
        error = self.rpc("does/not/exist").json()["error"]
        self.assertEqual(error["code"], protocol.METHOD_NOT_FOUND)

    def test_unknown_tool_is_a_jsonrpc_error(self):
        error = self.rpc("tools/call", {"name": "drop_database"}).json()["error"]
        self.assertEqual(error["code"], protocol.METHOD_NOT_FOUND)

    def test_malformed_json_is_a_parse_error(self):
        response = self.client.post(
            MCP_URL,
            data="{not json",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(response.json()["error"]["code"], protocol.PARSE_ERROR)

    def test_response_id_matches_the_request(self):
        self.assertEqual(self.rpc("ping", request_id=4242).json()["id"], 4242)

    def test_batch_requests_are_still_answered(self):
        """Batching left the spec in 2025-06-18; older clients still send it."""
        response = self.client.post(
            MCP_URL,
            data=json.dumps(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                ]
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        payload = response.json()
        self.assertEqual([item["id"] for item in payload], [1, 2])


class FindAvailableSlotsTests(McpWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_lists_free_slots_with_machine_ids(self):
        text, is_error = self.call_tool(
            "find_available_slots", {"date": self.tomorrow.isoformat()}
        )
        self.assertFalse(is_error)
        self.assertIn("free slot", text)
        self.assertIn(f"machine_id={self.washer.id}", text)
        self.assertIn(f"machine_id={self.dryer.id}", text)

    def test_kind_filter(self):
        text, _ = self.call_tool(
            "find_available_slots",
            {"date": self.tomorrow.isoformat(), "kind": "washer"},
        )
        self.assertIn(f"machine_id={self.washer.id}", text)
        self.assertNotIn(f"machine_id={self.dryer.id}", text)

    def test_kind_filter_accepts_plurals(self):
        text, is_error = self.call_tool(
            "find_available_slots",
            {"date": self.tomorrow.isoformat(), "kind": "dryers"},
        )
        self.assertFalse(is_error)
        self.assertIn(f"machine_id={self.dryer.id}", text)

    def test_time_range_filter(self):
        text, _ = self.call_tool(
            "find_available_slots",
            {"date": self.tomorrow.isoformat(), "after": "18", "before": "20"},
        )
        self.assertIn("18:00", text)
        self.assertIn("19:00", text)
        self.assertNotIn("09:00", text)

    def test_understands_pm_times(self):
        text, is_error = self.call_tool(
            "find_available_slots",
            {"date": self.tomorrow.isoformat(), "after": "6pm", "before": "8pm"},
        )
        self.assertFalse(is_error)
        self.assertIn("18:00", text)
        self.assertNotIn("09:00", text)

    def test_understands_tomorrow(self):
        text, is_error = self.call_tool(
            "find_available_slots", {"date": "tomorrow", "kind": "washer"}
        )
        self.assertFalse(is_error)
        self.assertIn(self.tomorrow.isoformat(), text)

    def test_taken_slots_are_not_offered(self):
        starts = timezone.make_aware(
            timezone.datetime.combine(self.tomorrow, time(14, 0))
        )
        other = self.make_student("riya@gim.ac.in", "Riya", Gender.MALE)
        Booking.objects.create(
            student=other,
            machine=self.washer,
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
        )
        text, _ = self.call_tool(
            "find_available_slots",
            {"date": self.tomorrow.isoformat(), "kind": "washer", "after": "14", "before": "15"},
        )
        self.assertIn("No free slots", text)

    def test_offline_machines_are_not_offered(self):
        self.washer.is_offline = True
        self.washer.save(update_fields=["is_offline"])
        text, _ = self.call_tool(
            "find_available_slots",
            {"date": self.tomorrow.isoformat(), "kind": "washer"},
        )
        self.assertIn("No free slots", text)

    def test_only_the_students_own_hostel_is_visible(self):
        other_hostel_machine = Machine.objects.create(
            hostel=self.girls,
            kind=MachineKind.WASHER,
            location_name="Girls Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        text, _ = self.call_tool(
            "find_available_slots", {"date": self.tomorrow.isoformat()}
        )
        self.assertNotIn(str(other_hostel_machine.id), text)

    def test_past_dates_are_refused(self):
        text, is_error = self.call_tool(
            "find_available_slots",
            {"date": (timezone.localdate() - timedelta(days=1)).isoformat()},
        )
        self.assertTrue(is_error)
        self.assertIn("past", text)

    def test_bad_date_is_explained(self):
        text, is_error = self.call_tool("find_available_slots", {"date": "next thursday"})
        self.assertTrue(is_error)
        self.assertIn("YYYY-MM-DD", text)

    def test_bad_kind_is_explained(self):
        text, is_error = self.call_tool(
            "find_available_slots", {"date": "tomorrow", "kind": "microwave"}
        )
        self.assertTrue(is_error)
        self.assertIn("washer", text)


class BookSlotTests(McpWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_books_a_slot(self):
        text, is_error = self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.washer.id),
                "date": self.tomorrow.isoformat(),
                "hour": "14",
            },
        )
        self.assertFalse(is_error)
        self.assertIn("Booked", text)
        booking = Booking.objects.get()
        self.assertEqual(booking.student, self.student)
        self.assertEqual(booking.machine, self.washer)
        self.assertEqual(timezone.localtime(booking.starts_at).hour, 14)
        self.assertIn(f"booking_id={booking.id}", text)

    def test_books_from_an_iso_timestamp(self):
        text, is_error = self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.washer.id),
                "starts_at": f"{self.tomorrow.isoformat()}T09:00",
            },
        )
        self.assertFalse(is_error)
        self.assertEqual(timezone.localtime(Booking.objects.get().starts_at).hour, 9)

    def test_find_then_book_round_trip(self):
        """The flow a chat actually follows: search, then book what it found."""
        listing, _ = self.call_tool(
            "find_available_slots",
            {"date": self.tomorrow.isoformat(), "kind": "washer", "after": "20", "before": "21"},
        )
        machine_id = listing.split("machine_id=")[1].split()[0].strip()
        text, is_error = self.call_tool(
            "book_slot",
            {"machine_id": machine_id, "date": self.tomorrow.isoformat(), "hour": "20"},
        )
        self.assertFalse(is_error)
        self.assertEqual(timezone.localtime(Booking.objects.get().starts_at).hour, 20)

    def test_double_booking_is_refused(self):
        other = self.make_student("riya@gim.ac.in", "Riya", Gender.MALE)
        starts = timezone.make_aware(
            timezone.datetime.combine(self.tomorrow, time(14, 0))
        )
        Booking.objects.create(
            student=other,
            machine=self.washer,
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
        )
        text, is_error = self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.washer.id),
                "date": self.tomorrow.isoformat(),
                "hour": "14",
            },
        )
        self.assertTrue(is_error)
        self.assertIn("SLOT_TAKEN", text)
        self.assertEqual(Booking.objects.count(), 1)

    def test_offline_machine_is_refused(self):
        self.washer.is_offline = True
        self.washer.save(update_fields=["is_offline"])
        text, is_error = self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.washer.id),
                "date": self.tomorrow.isoformat(),
                "hour": "14",
            },
        )
        self.assertTrue(is_error)
        self.assertIn("MACHINE_OFFLINE", text)

    def test_quota_is_enforced_over_mcp(self):
        """The institute's quota applies to a chat booking exactly as in-app."""
        for hour in (8, 9, 10):
            starts = timezone.make_aware(
                timezone.datetime.combine(self.tomorrow, time(hour, 0))
            )
            Booking.objects.create(
                student=self.student,
                machine=self.washer,
                starts_at=starts,
                ends_at=starts + timedelta(hours=1),
                counts_against_quota=True,
            )
        text, is_error = self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.washer.id),
                "date": self.tomorrow.isoformat(),
                "hour": "15",
            },
        )
        self.assertTrue(is_error)
        self.assertIn("RULE_BLOCKED", text)
        self.assertEqual(Booking.objects.count(), 3)

    def test_suspended_student_cannot_book(self):
        self.student.suspension_ends = timezone.now() + timedelta(days=2)
        self.student.save(update_fields=["suspension_ends"])
        text, is_error = self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.washer.id),
                "date": self.tomorrow.isoformat(),
                "hour": "14",
            },
        )
        self.assertTrue(is_error)
        self.assertIn("SUSPENDED", text)
        self.assertEqual(Booking.objects.count(), 0)

    def test_unverified_student_cannot_book(self):
        self.student.email_verified_at = None
        self.student.save(update_fields=["email_verified_at"])
        text, is_error = self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.washer.id),
                "date": self.tomorrow.isoformat(),
                "hour": "14",
            },
        )
        self.assertTrue(is_error)
        self.assertIn("UNVERIFIED", text)

    def test_another_hostels_machine_is_refused(self):
        foreign = Machine.objects.create(
            hostel=self.girls,
            kind=MachineKind.WASHER,
            location_name="Girls Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        text, is_error = self.call_tool(
            "book_slot",
            {
                "machine_id": str(foreign.id),
                "date": self.tomorrow.isoformat(),
                "hour": "14",
            },
        )
        self.assertTrue(is_error)
        self.assertIn("NOT_FOUND", text)
        self.assertEqual(Booking.objects.count(), 0)

    def test_missing_machine_id_is_explained(self):
        text, is_error = self.call_tool("book_slot", {"hour": "14"})
        self.assertTrue(is_error)
        self.assertIn("machine_id", text)

    def test_missing_time_is_explained(self):
        text, is_error = self.call_tool(
            "book_slot", {"machine_id": str(self.washer.id)}
        )
        self.assertTrue(is_error)
        self.assertIn("starts_at", text)

    def test_unknown_arguments_are_ignored(self):
        """Models invent extra keys; that should not fail an otherwise-valid call."""
        text, is_error = self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.washer.id),
                "date": self.tomorrow.isoformat(),
                "hour": "14",
                "confirm": True,
                "notes": "please be quick",
            },
        )
        self.assertFalse(is_error)
        self.assertEqual(Booking.objects.count(), 1)


class BookingManagementToolTests(McpWorldMixin, TestCase):
    def setUp(self):
        self.make_world()
        starts = timezone.make_aware(
            timezone.datetime.combine(self.tomorrow, time(14, 0))
        )
        self.booking = Booking.objects.create(
            student=self.student,
            machine=self.washer,
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
            counts_against_quota=True,
        )

    def test_list_my_bookings(self):
        text, is_error = self.call_tool("list_my_bookings")
        self.assertFalse(is_error)
        self.assertIn(f"booking_id={self.booking.id}", text)
        self.assertIn("Quota: 1/3", text)

    def test_list_is_empty_when_nothing_is_booked(self):
        self.booking.delete()
        text, is_error = self.call_tool("list_my_bookings")
        self.assertFalse(is_error)
        self.assertIn("No upcoming bookings", text)

    def test_list_excludes_other_students_bookings(self):
        other = self.make_student("riya@gim.ac.in", "Riya", Gender.MALE)
        starts = timezone.make_aware(
            timezone.datetime.combine(self.tomorrow, time(16, 0))
        )
        theirs = Booking.objects.create(
            student=other,
            machine=self.washer,
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
        )
        text, _ = self.call_tool("list_my_bookings")
        self.assertNotIn(str(theirs.id), text)

    def test_cancel_booking(self):
        text, is_error = self.call_tool(
            "cancel_booking", {"booking_id": str(self.booking.id)}
        )
        self.assertFalse(is_error)
        self.assertIn("Cancelled", text)
        self.booking.refresh_from_db()
        self.assertIsNotNone(self.booking.cancelled_at)

    def test_cannot_cancel_another_students_booking(self):
        other = self.make_student("riya@gim.ac.in", "Riya", Gender.MALE)
        starts = timezone.make_aware(
            timezone.datetime.combine(self.tomorrow, time(16, 0))
        )
        theirs = Booking.objects.create(
            student=other,
            machine=self.washer,
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
        )
        text, is_error = self.call_tool(
            "cancel_booking", {"booking_id": str(theirs.id)}
        )
        self.assertTrue(is_error)
        self.assertIn("NOT_FOUND", text)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.cancelled_at)

    def test_cancel_unknown_booking(self):
        text, is_error = self.call_tool(
            "cancel_booking", {"booking_id": str(uuid.uuid4())}
        )
        self.assertTrue(is_error)
        self.assertIn("NOT_FOUND", text)

    def test_cancel_without_an_id_is_explained(self):
        text, is_error = self.call_tool("cancel_booking", {})
        self.assertTrue(is_error)
        self.assertIn("booking_id", text)


class TwoStudentsAreIsolatedTests(McpWorldMixin, TestCase):
    """A token acts for exactly one student — never for whoever else asks."""

    def setUp(self):
        self.make_world()
        self.other = self.make_student("riya@gim.ac.in", "Riya", Gender.MALE)
        _, self.other_token = McpToken.issue(student=self.other, name="ChatGPT")

    def test_each_token_books_for_its_own_student(self):
        self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.washer.id),
                "date": self.tomorrow.isoformat(),
                "hour": "14",
            },
        )
        self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.dryer.id),
                "date": self.tomorrow.isoformat(),
                "hour": "15",
            },
            token=self.other_token,
        )
        self.assertEqual(
            Booking.objects.get(machine=self.washer).student, self.student
        )
        self.assertEqual(Booking.objects.get(machine=self.dryer).student, self.other)

    def test_each_token_sees_only_its_own_bookings(self):
        self.call_tool(
            "book_slot",
            {
                "machine_id": str(self.washer.id),
                "date": self.tomorrow.isoformat(),
                "hour": "14",
            },
        )
        text, _ = self.call_tool("list_my_bookings", token=self.other_token)
        self.assertIn("No upcoming bookings", text)


class McpTokenApiTests(McpWorldMixin, TestCase):
    """The REST endpoints a student uses to mint and revoke connector tokens."""

    def setUp(self):
        self.make_world()
        self.api = APIClient()
        self.api.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {RefreshToken.for_user(self.student.user).access_token}"
            )
        )

    def test_create_returns_the_plaintext_once(self):
        response = self.api.post(
            "/api/v1/me/mcp-tokens", {"name": "ChatGPT"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        plaintext = response.data["token"]
        self.assertTrue(plaintext.startswith(TOKEN_PREFIX))
        self.assertEqual(McpToken.resolve(plaintext).name, "ChatGPT")

        # It is never readable again.
        listing = self.api.get("/api/v1/me/mcp-tokens")
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertNotIn("token", listing.data["results"][0])

    def test_list_shows_only_the_callers_tokens(self):
        McpToken.issue(student=self.other_student(), name="Someone else")
        response = self.api.get("/api/v1/me/mcp-tokens")
        names = {row["name"] for row in response.data["results"]}
        self.assertEqual(names, {"Claude desktop"})

    def other_student(self):
        return self.make_student("riya@gim.ac.in", "Riya", Gender.MALE)

    def test_revoke_kills_the_token(self):
        response = self.api.delete(f"/api/v1/me/mcp-tokens/{self.token_row.id}")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertIsNone(McpToken.resolve(self.token))
        self.assertEqual(self.rpc("ping").status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cannot_revoke_someone_elses_token(self):
        their_row, their_token = McpToken.issue(
            student=self.other_student(), name="Theirs"
        )
        response = self.api.delete(f"/api/v1/me/mcp-tokens/{their_row.id}")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIsNotNone(McpToken.resolve(their_token))

    def test_expiry_must_be_in_the_future(self):
        response = self.api.post(
            "/api/v1/me/mcp-tokens",
            {
                "name": "Stale",
                "expires_at": (timezone.now() - timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_anonymous_cannot_manage_tokens(self):
        anon = APIClient()
        self.assertEqual(
            anon.get("/api/v1/me/mcp-tokens").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_routes_are_registered(self):
        self.assertTrue(reverse("student-mcp-tokens"))
        self.assertTrue(reverse("mcp-endpoint"))
        self.assertTrue(
            reverse("student-mcp-token-revoke", kwargs={"token_id": self.token_row.id})
        )
