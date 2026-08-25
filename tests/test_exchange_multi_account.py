"""
Agent D — API multi-account QA for the seven plan scenarios.

Pilot identities (password unused in TestCase; JWT via RefreshToken):
  A: aarav.mehta@gim.ac.in
  B: rohan.shetty@gim.ac.in
  C: diya.nair@gim.ac.in
"""

from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from laundry.models import (
    ExchangeStatus,
    Gender,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    MachineKind,
    Student,
)

User = get_user_model()


def aware(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute))


class MultiAccountExchangeAPITests(TestCase):
    """Scenarios 1–7 from fix_slot_exchanges plan (Agent D)."""

    def setUp(self):
        self.institute = Institute.objects.create(
            name="GIM Multi-Account QA",
            allowed_email_domains=["gim.ac.in"],
        )
        InstituteRule.objects.create(
            institute=self.institute,
            quota_limit=5,
            quota_window_days=7,
            cooldown_hours=0,
            advance_window_days=7,
            cancellation_cutoff_hours=6,
            dryer_cap_enabled=False,
        )
        self.boys = Hostel.objects.create(institute=self.institute, name="Boys 1")
        self.washer = Machine.objects.create(
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

        self.aarav = self._student("aarav.mehta@gim.ac.in", "Aarav Mehta")
        self.rohan = self._student("rohan.shetty@gim.ac.in", "Rohan Shetty")
        self.diya = self._student("diya.nair@gim.ac.in", "Diya Nair")

        self.client_a = self._auth(self.aarav)
        self.client_b = self._auth(self.rohan)
        self.client_c = self._auth(self.diya)

    def _student(self, email, name):
        user = User.objects.create_user(email=email, password="LundriiStudent!1")
        return Student.objects.create(
            user=user,
            institute=self.institute,
            name=name,
            gender=Gender.MALE,
            home_hostel=self.boys,
            email_verified_at=timezone.now(),
        )

    def _auth(self, student):
        client = APIClient()
        token = RefreshToken.for_user(student.user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def _booking(self, student, machine, starts_at, *, counts=True):
        from laundry.models import Booking

        return Booking.objects.create(
            student=student,
            machine=machine,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            counts_against_quota=counts,
        )

    def _future_start(self, hours=10):
        return (timezone.localtime() + timedelta(hours=hours)).replace(
            minute=0, second=0, microsecond=0
        )

    def _day_start(self, *, days_ahead: int, hour: int):
        local = timezone.localtime()
        return (local + timedelta(days=days_ahead)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )

    def _pending_incoming_count(self, client):
        home = client.get("/api/v1/home")
        self.assertEqual(home.status_code, 200, home.data)
        return home.data["pendingIncomingExchangeCount"]

    def _create_request(self, client, target_id, *, kind="request", offered_id=None):
        body = {"kind": kind, "targetBookingId": str(target_id)}
        if offered_id is not None:
            body["offeredBookingId"] = str(offered_id)
        return client.post("/api/v1/exchanges", body, format="json")

    # --- Scenario 1 ---
    def test_scenario_1_request_approve_transfers_ownership(self):
        """A requests B's future slot → B approves → A owns; B does not."""
        target = self._booking(self.rohan, self.washer, self._future_start(12))
        created = self._create_request(self.client_a, target.id)
        self.assertEqual(created.status_code, 201, created.data)
        exchange_id = created.data["id"]

        approved = self.client_b.post(f"/api/v1/exchanges/{exchange_id}/approve")
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["status"], "approved")

        target.refresh_from_db()
        self.assertEqual(target.student_id, self.aarav.pk)
        self.assertNotEqual(target.student_id, self.rohan.pk)

        a_upcoming = self.client_a.get("/api/v1/bookings", {"status": "upcoming"})
        b_upcoming = self.client_b.get("/api/v1/bookings", {"status": "upcoming"})
        a_ids = {row["id"] for row in a_upcoming.data["results"]}
        b_ids = {row["id"] for row in b_upcoming.data["results"]}
        self.assertIn(str(target.id), a_ids)
        self.assertNotIn(str(target.id), b_ids)

    # --- Scenario 2 ---
    def test_scenario_2_request_reject_keeps_ownership(self):
        """A requests → B rejects → ownership unchanged."""
        target = self._booking(self.rohan, self.washer, self._future_start(14))
        created = self._create_request(self.client_a, target.id)
        self.assertEqual(created.status_code, 201, created.data)

        rejected = self.client_b.post(
            f"/api/v1/exchanges/{created.data['id']}/reject",
            {"note": "Keeping this one."},
            format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertEqual(rejected.data["status"], "rejected")

        target.refresh_from_db()
        self.assertEqual(target.student_id, self.rohan.pk)

    # --- Scenario 3 ---
    def test_scenario_3_swap_approve_swaps_both(self):
        """A swap-offers for B → B approves → both bookings swapped."""
        target = self._booking(self.rohan, self.washer, self._future_start(16))
        offered = self._booking(self.aarav, self.washer_b, self._future_start(18))
        created = self._create_request(
            self.client_a, target.id, kind="swap", offered_id=offered.id
        )
        self.assertEqual(created.status_code, 201, created.data)

        approved = self.client_b.post(
            f"/api/v1/exchanges/{created.data['id']}/approve"
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["status"], "approved")

        target.refresh_from_db()
        offered.refresh_from_db()
        self.assertEqual(target.student_id, self.aarav.pk)
        self.assertEqual(offered.student_id, self.rohan.pk)

    # --- Scenario 4 ---
    def test_scenario_4_withdraw_clears_pending(self):
        """A sends → A withdraws → pending cleared; B unchanged."""
        target = self._booking(self.rohan, self.washer, self._future_start(11))
        created = self._create_request(self.client_a, target.id)
        self.assertEqual(created.status_code, 201, created.data)
        exchange_id = created.data["id"]

        self.assertEqual(self._pending_incoming_count(self.client_b), 1)

        withdrawn = self.client_a.post(f"/api/v1/exchanges/{exchange_id}/withdraw")
        self.assertEqual(withdrawn.status_code, 200, withdrawn.data)
        self.assertEqual(withdrawn.data["status"], "rejected")
        self.assertIn("Withdrawn", withdrawn.data.get("failureReason") or "")

        target.refresh_from_db()
        self.assertEqual(target.student_id, self.rohan.pk)
        self.assertEqual(self._pending_incoming_count(self.client_b), 0)

        outgoing = self.client_a.get(
            "/api/v1/exchanges", {"direction": "outgoing"}
        )
        pending_out = [
            r
            for r in outgoing.data["results"]
            if r["status"] == ExchangeStatus.PENDING
            or r["status"] == "pending"
        ]
        self.assertEqual(pending_out, [])

    # --- Scenario 5 ---
    def test_scenario_5_cross_day_tomorrow_target(self):
        """Cross-day target (tomorrow) uses the correct booking id."""
        tomorrow = self._booking(
            self.rohan, self.washer, self._day_start(days_ahead=1, hour=15)
        )
        other_day = self._booking(
            self.rohan, self.washer, self._day_start(days_ahead=2, hour=15)
        )
        self.assertNotEqual(
            timezone.localtime(tomorrow.starts_at).date(),
            timezone.localtime(other_day.starts_at).date(),
        )

        created = self._create_request(self.client_a, tomorrow.id)
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["targetBooking"]["id"], str(tomorrow.id))
        self.assertNotEqual(created.data["targetBooking"]["id"], str(other_day.id))

        outgoing = self.client_a.get(
            "/api/v1/exchanges", {"direction": "outgoing"}
        )
        self.assertEqual(outgoing.status_code, 200, outgoing.data)
        match = next(
            r for r in outgoing.data["results"] if r["id"] == created.data["id"]
        )
        self.assertEqual(match["targetBooking"]["id"], str(tomorrow.id))

    # --- Scenario 6 ---
    def test_scenario_6_competing_pending_expires_after_approve(self):
        """A and C both pending on B → B approves A → C pending expired/failed."""
        target = self._booking(self.rohan, self.washer, self._future_start(13))
        from_a = self._create_request(self.client_a, target.id)
        from_c = self._create_request(self.client_c, target.id)
        self.assertEqual(from_a.status_code, 201, from_a.data)
        self.assertEqual(from_c.status_code, 201, from_c.data)
        self.assertEqual(self._pending_incoming_count(self.client_b), 2)

        approved = self.client_b.post(
            f"/api/v1/exchanges/{from_a.data['id']}/approve"
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["status"], "approved")

        target.refresh_from_db()
        self.assertEqual(target.student_id, self.aarav.pk)

        # Listing as C runs expire_stale; competing request must clear.
        c_out = self.client_c.get("/api/v1/exchanges", {"direction": "outgoing"})
        self.assertEqual(c_out.status_code, 200, c_out.data)
        c_row = next(r for r in c_out.data["results"] if r["id"] == from_c.data["id"])
        self.assertIn(c_row["status"], ("expired", "failed"))

        b_incoming = self.client_b.get(
            "/api/v1/exchanges", {"direction": "incoming"}
        )
        pending_b = [
            r for r in b_incoming.data["results"] if r["status"] == "pending"
        ]
        self.assertEqual(pending_b, [])
        self.assertEqual(self._pending_incoming_count(self.client_b), 0)

    # --- Scenario 7 ---
    def test_scenario_7_holder_pending_count_matches_inbox(self):
        """Holder pending count / inbox matches pending exchanges."""
        t1 = self._booking(self.rohan, self.washer, self._future_start(9))
        t2 = self._booking(self.rohan, self.washer_b, self._future_start(17))
        self.assertEqual(self._pending_incoming_count(self.client_b), 0)

        r1 = self._create_request(self.client_a, t1.id)
        r2 = self._create_request(self.client_c, t2.id)
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertEqual(r2.status_code, 201, r2.data)

        incoming = self.client_b.get(
            "/api/v1/exchanges", {"direction": "incoming"}
        )
        self.assertEqual(incoming.status_code, 200, incoming.data)
        pending = [r for r in incoming.data["results"] if r["status"] == "pending"]
        self.assertEqual(len(pending), 2)
        self.assertEqual(incoming.data["count"], 2)
        self.assertEqual(self._pending_incoming_count(self.client_b), 2)

        # Reject one → count drops to 1 and matches inbox.
        rejected = self.client_b.post(
            f"/api/v1/exchanges/{r1.data['id']}/reject",
            {"note": "No"},
            format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)

        incoming2 = self.client_b.get(
            "/api/v1/exchanges", {"direction": "incoming"}
        )
        pending2 = [
            r for r in incoming2.data["results"] if r["status"] == "pending"
        ]
        self.assertEqual(len(pending2), 1)
        self.assertEqual(self._pending_incoming_count(self.client_b), 1)
        self.assertEqual(pending2[0]["id"], r2.data["id"])
