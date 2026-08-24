"""Focused tests for GET /home bootstrap."""

from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from laundry.models import (
    Booking,
    Exchange,
    ExchangeKind,
    ExchangeStatus,
    Gender,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    MachineKind,
    Student,
)
from laundry.services.exchanges import create_exchange

User = get_user_model()


class HomeAPITests(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(
            name="GIM Home Test",
            allowed_email_domains=["gim.ac.in"],
        )
        self.other_institute = Institute.objects.create(
            name="Other Institute",
            allowed_email_domains=["other.edu"],
        )
        InstituteRule.objects.create(
            institute=self.institute,
            quota_limit=3,
            quota_window_days=7,
            cooldown_hours=0,
            advance_window_days=7,
            cancellation_cutoff_hours=6,
            dryer_cap_enabled=False,
        )
        self.boys = Hostel.objects.create(
            institute=self.institute, name="Boys 1"
        )
        self.boys_two = Hostel.objects.create(
            institute=self.institute, name="Boys 2"
        )
        self.foreign = Hostel.objects.create(
            institute=self.other_institute, name="Foreign Hall"
        )
        self.washer_a = Machine.objects.create(
            hostel=self.boys,
            kind=MachineKind.WASHER,
            location_name="3rd Floor · A Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.washer_b = Machine.objects.create(
            hostel=self.boys,
            kind=MachineKind.WASHER,
            location_name="2nd Floor · C Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.dryer = Machine.objects.create(
            hostel=self.boys,
            kind=MachineKind.DRYER,
            location_name="Ground · Dryer",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.boys_two_washer = Machine.objects.create(
            hostel=self.boys_two,
            kind=MachineKind.WASHER,
            location_name="Boys 2 · Washer",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        Machine.objects.create(
            hostel=self.foreign,
            kind=MachineKind.WASHER,
            location_name="Foreign Washer",
        )
        self.now = timezone.now().replace(microsecond=0)
        self.user = User.objects.create_user(
            email="aarav@gim.ac.in", password="unused"
        )
        self.student = Student.objects.create(
            user=self.user,
            institute=self.institute,
            name="Aarav Mehta",
            gender=Gender.MALE,
            home_hostel=self.boys,
            email_verified_at=self.now,
        )
        self.peer_user = User.objects.create_user(
            email="priya@gim.ac.in", password="unused"
        )
        self.peer = Student.objects.create(
            user=self.peer_user,
            institute=self.institute,
            name="Priya Kulkarni",
            gender=Gender.MALE,
            home_hostel=self.boys,
            email_verified_at=self.now,
        )
        self.client = APIClient()
        token = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def _add_booking(self, student, machine, starts_at):
        return Booking.objects.create(
            student=student,
            machine=machine,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            counts_against_quota=True,
        )

    def _availability_washer_counts(self, hostel_id):
        response = self.client.get(f"/api/v1/hostels/{hostel_id}/availability/now")
        self.assertEqual(response.status_code, 200, response.data)
        washers = response.data["washers"]
        return washers["freeNow"], washers["total"]

    def test_signed_in_home_includes_profile_machines_washers_upcoming(self):
        start = (timezone.localtime() + timedelta(hours=8)).replace(
            minute=0, second=0, microsecond=0
        )
        self._add_booking(self.student, self.washer_a, start)
        self._add_booking(
            self.student, self.washer_b, start + timedelta(hours=2)
        )
        self._add_booking(
            self.student, self.dryer, start + timedelta(hours=4)
        )
        free_now, total = self._availability_washer_counts(self.boys.id)

        response = self.client.get("/api/v1/home")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data

        self.assertIsNotNone(data["profile"])
        self.assertTrue(data["profile"]["emailVerified"])
        self.assertEqual(data["profile"]["name"], "Aarav Mehta")
        self.assertEqual(str(data["selectedHostelId"]), str(self.boys.id))
        self.assertEqual(data["washersTotal"], total)
        self.assertEqual(data["washersFree"], free_now)
        self.assertEqual(data["washersTotal"], 2)
        self.assertEqual(len(data["machines"]), 3)
        self.assertEqual(len(data["upcoming"]), 2)
        self.assertEqual(data["pendingIncomingExchangeCount"], 0)
        hostel_ids = {str(h["id"]) for h in data["hostels"]}
        self.assertEqual(
            hostel_ids, {str(self.boys.id), str(self.boys_two.id)}
        )
        home = next(h for h in data["hostels"] if str(h["id"]) == str(self.boys.id))
        self.assertTrue(home["isHome"])

    def test_guest_omits_profile_and_private_fields(self):
        anon = APIClient()
        response = anon.get("/api/v1/home")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertIsNone(data["profile"])
        self.assertEqual(data["upcoming"], [])
        self.assertEqual(data["pendingIncomingExchangeCount"], 0)
        self.assertGreaterEqual(data["washersTotal"], 1)
        self.assertIn("machines", data)
        self.assertIn("hostels", data)
        self.assertIsNotNone(data["selectedHostelId"])

    def test_hostel_id_switches_machines_and_washer_counts(self):
        free_now, total = self._availability_washer_counts(self.boys_two.id)
        response = self.client.get(
            "/api/v1/home", {"hostelId": str(self.boys_two.id)}
        )
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual(str(data["selectedHostelId"]), str(self.boys_two.id))
        self.assertEqual(data["washersTotal"], total)
        self.assertEqual(data["washersFree"], free_now)
        self.assertEqual(data["washersTotal"], 1)
        self.assertEqual(len(data["machines"]), 1)
        self.assertEqual(
            str(data["machines"][0]["id"]), str(self.boys_two_washer.id)
        )

    def test_ineligible_hostel_404(self):
        response = self.client.get(
            "/api/v1/home", {"hostelId": str(self.foreign.id)}
        )
        self.assertEqual(response.status_code, 404)

    def test_unknown_hostel_404(self):
        response = self.client.get(
            "/api/v1/home",
            {"hostelId": "00000000-0000-0000-0000-000000000099"},
        )
        self.assertEqual(response.status_code, 404)

    def test_pending_incoming_exchange_count(self):
        start = (timezone.localtime() + timedelta(hours=10)).replace(
            minute=0, second=0, microsecond=0
        )
        target = self._add_booking(self.student, self.washer_a, start)
        create_exchange(
            self.peer,
            kind=ExchangeKind.REQUEST,
            target_booking_id=target.id,
        )
        response = self.client.get("/api/v1/home")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["pendingIncomingExchangeCount"], 1)
        self.assertEqual(
            Exchange.objects.filter(status=ExchangeStatus.PENDING).count(), 1
        )

    def test_washer_counts_match_machine_card_statuses(self):
        response = self.client.get("/api/v1/home")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        washers = [m for m in data["machines"] if m["kind"] == MachineKind.WASHER]
        free_from_cards = sum(1 for m in washers if m["status"] == "free")
        self.assertEqual(data["washersTotal"], len(washers))
        self.assertEqual(data["washersFree"], free_from_cards)
        self.assertEqual(len(data["machines"]), 3)
        kinds = {m["kind"] for m in data["machines"]}
        self.assertIn(MachineKind.DRYER, kinds)
