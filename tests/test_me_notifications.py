"""Focused tests for GET/PATCH /me and notification read APIs."""

from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
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
    Notification,
    NotificationKind,
    NotificationType,
    Strike,
    Student,
    Ticket,
    TicketKind,
)
from laundry.services.rules import cooldown_clears_at, quota_status

User = get_user_model()


def aware(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute))


class MeAndNotificationAPITests(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(
            name="GIM Test",
            allowed_email_domains=["gim.ac.in", "student.gim.ac.in"],
        )
        self.rules = InstituteRule.objects.create(
            institute=self.institute,
            quota_limit=3,
            quota_window_days=7,
            cooldown_hours=0,
            advance_window_days=7,
            cancellation_cutoff_hours=6,
            dryer_cap_enabled=False,
        )
        self.boys = Hostel.objects.create(
            institute=self.institute, name="Boys 1", gender=Gender.MALE
        )
        self.boys_two = Hostel.objects.create(
            institute=self.institute, name="Boys 2", gender=Gender.MALE
        )
        self.girls = Hostel.objects.create(
            institute=self.institute, name="Girls 1", gender=Gender.FEMALE
        )
        self.washer = Machine.objects.create(
            hostel=self.boys,
            kind=MachineKind.WASHER,
            location_name="3rd Floor · A Wing",
        )
        self.user = User.objects.create_user(
            email="aarav@gim.ac.in", password="unused"
        )
        self.now = timezone.now().replace(microsecond=0)
        self.student = Student.objects.create(
            user=self.user,
            institute=self.institute,
            name="Aarav Mehta",
            phone="+91 98220 41127",
            whatsapp_opt_in=True,
            gender=Gender.MALE,
            home_hostel=self.boys,
            floor="3rd Floor",
            email_verified_at=self.now,
        )
        self.client = APIClient()
        token = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def _add_washer(self, starts_at, *, cancelled_at=None, counts=True):
        return Booking.objects.create(
            student=self.student,
            machine=self.washer,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            cancelled_at=cancelled_at,
            counts_against_quota=counts,
        )

    def test_me_unauthenticated(self):
        anon = APIClient()
        response = anon.get("/api/v1/me")
        self.assertEqual(response.status_code, 401)

    def test_get_me_profile_quota_and_strikes(self):
        past = self.now - timedelta(days=2)
        self._add_washer(past.replace(minute=0, second=0, microsecond=0))
        upcoming = (self.now + timedelta(hours=8)).replace(
            minute=0, second=0, microsecond=0
        )
        self._add_washer(upcoming)

        ticket = Ticket.objects.create(
            student=self.student,
            kind=TicketKind.MAINTENANCE,
            number=427,
            machine=self.washer,
            student_note="left in drum",
        )
        strike = Strike.objects.create(
            student=self.student,
            reason="Laundry left in the drum for two days.",
            date=(self.now - timedelta(days=10)).date(),
            recorded_by=self.user,
            ticket=ticket,
        )

        response = self.client.get("/api/v1/me")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual(data["name"], "Aarav Mehta")
        self.assertEqual(data["email"], "aarav@gim.ac.in")
        self.assertEqual(data["phone"], "+91 98220 41127")
        self.assertEqual(str(data["hostelId"]), str(self.boys.id))
        self.assertEqual(data["hostelName"], "Boys 1")
        self.assertEqual(data["floor"], "3rd Floor")
        self.assertEqual(data["gender"], Gender.MALE)
        self.assertTrue(data["emailVerified"])
        self.assertFalse(data["suspended"])
        self.assertIsNone(data["suspensionEnds"])
        self.assertEqual(data["quota"]["used"], 2)
        self.assertEqual(data["quota"]["limit"], 3)
        self.assertEqual(data["quota"]["windowDays"], 7)
        self.assertIsNotNone(data["quota"]["resetsAt"])
        self.assertEqual(len(data["strikes"]), 1)
        self.assertEqual(str(data["strikes"][0]["id"]), str(strike.id))
        self.assertEqual(data["strikes"][0]["ticketNumber"], 427)
        self.assertIn("Laundry left", data["strikes"][0]["reason"])

    def test_patch_me_basic_fields(self):
        response = self.client.patch(
            "/api/v1/me",
            {
                "name": "Aarav M.",
                "phone": "+91 90000 00000",
                "whatsappOptIn": False,
                "gender": Gender.FEMALE,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["name"], "Aarav M.")
        self.assertEqual(response.data["phone"], "+91 90000 00000")
        self.assertFalse(response.data["whatsappOptIn"])
        self.assertEqual(response.data["gender"], Gender.MALE)
        self.assertEqual(str(response.data["hostelId"]), str(self.boys.id))

        self.student.refresh_from_db()
        self.assertEqual(self.student.name, "Aarav M.")
        self.assertEqual(self.student.gender, Gender.MALE)
        self.assertEqual(self.student.home_hostel_id, self.boys.id)
        self.assertFalse(self.student.whatsapp_opt_in)

    def test_patch_me_updates_hostel_and_floor(self):
        response = self.client.patch(
            "/api/v1/me",
            {
                "hostelId": str(self.boys_two.id),
                "floor": "3rd Floor",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(str(response.data["hostelId"]), str(self.boys_two.id))
        self.assertEqual(response.data["hostelName"], "Boys 2")
        self.assertEqual(response.data["floor"], "3rd Floor")

        self.student.refresh_from_db()
        self.assertEqual(self.student.home_hostel_id, self.boys_two.id)
        self.assertEqual(self.student.floor, "3rd Floor")

    def test_patch_me_rejects_opposite_gender_hostel(self):
        response = self.client.patch(
            "/api/v1/me",
            {"hostelId": str(self.girls.id), "floor": "3rd Floor"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.student.refresh_from_db()
        self.assertEqual(self.student.home_hostel_id, self.boys.id)

    def test_patch_me_rejects_invalid_floor(self):
        response = self.client.patch(
            "/api/v1/me",
            {"floor": "99th Floor"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.student.refresh_from_db()
        self.assertEqual(self.student.floor, "3rd Floor")

    def test_patch_me_updates_floor_only(self):
        Machine.objects.create(
            hostel=self.boys,
            kind=MachineKind.WASHER,
            location_name="4th Floor · B Wing",
        )
        response = self.client.patch(
            "/api/v1/me",
            {"floor": "4th Floor"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["floor"], "4th Floor")
        self.student.refresh_from_db()
        self.assertEqual(self.student.floor, "4th Floor")

    def test_me_hostels_same_gender_with_is_home(self):
        response = self.client.get("/api/v1/me/hostels")
        self.assertEqual(response.status_code, 200, response.data)
        names = {row["name"]: row for row in response.data}
        self.assertIn("Boys 1", names)
        self.assertIn("Boys 2", names)
        self.assertNotIn("Girls 1", names)
        self.assertTrue(names["Boys 1"]["isHome"])
        self.assertFalse(names["Boys 2"]["isHome"])

    def test_me_hostels_when_gender_blank_uses_home_hostel(self):
        self.student.gender = ""
        self.student.save(update_fields=["gender", "updated_at"])
        response = self.client.get("/api/v1/me/hostels")
        self.assertEqual(response.status_code, 200, response.data)
        names = {row["name"] for row in response.data}
        self.assertIn("Boys 1", names)
        self.assertIn("Boys 2", names)
        self.assertNotIn("Girls 1", names)
        me = self.client.get("/api/v1/me")
        self.assertEqual(me.data["gender"], Gender.MALE)

    def test_patch_hostel_fills_blank_gender(self):
        self.student.gender = ""
        self.student.save(update_fields=["gender", "updated_at"])
        response = self.client.patch(
            "/api/v1/me",
            {"hostelId": str(self.boys_two.id), "floor": "3rd Floor"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["gender"], Gender.MALE)
        self.student.refresh_from_db()
        self.assertEqual(self.student.gender, Gender.MALE)
        self.assertEqual(self.student.home_hostel_id, self.boys_two.id)

    def test_me_institute_rules_and_domains(self):
        response = self.client.get("/api/v1/me/institute")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["name"], "GIM Test")
        self.assertIn("gim.ac.in", response.data["allowedDomains"])
        rules = response.data["rules"]
        self.assertEqual(rules["quotaLimit"], 3)
        self.assertEqual(rules["quotaWindowDays"], 7)
        self.assertEqual(rules["cooldownHours"], 0)
        self.assertFalse(rules["dryerCapEnabled"])

    def test_notifications_list_and_mark_read(self):
        first = Notification.objects.create(
            student=self.student,
            title="Swap approved",
            body="Priya accepted your offer.",
            type=NotificationType.EXCHANGE_OUTCOME,
            kind=NotificationKind.SUCCESS,
            related_object_type="exchange",
        )
        second = Notification.objects.create(
            student=self.student,
            title="Ticket #418 resolved",
            body="Committee took the machine offline.",
            type=NotificationType.TICKET_UPDATE,
            kind=NotificationKind.INFO,
            related_object_type="ticket",
        )

        listed = self.client.get("/api/v1/notifications")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertIn("results", listed.data)
        self.assertEqual(listed.data["count"], 2)
        self.assertFalse(listed.data["results"][0]["read"])
        self.assertIsNotNone(listed.data["results"][0]["deepLink"])

        marked = self.client.post(f"/api/v1/notifications/{first.id}/read")
        self.assertEqual(marked.status_code, 200, marked.data)
        self.assertTrue(marked.data["read"])
        first.refresh_from_db()
        self.assertIsNotNone(first.read_at)
        second.refresh_from_db()
        self.assertIsNone(second.read_at)

        read_all = self.client.post("/api/v1/notifications/read-all")
        self.assertEqual(read_all.status_code, 200, read_all.data)
        self.assertEqual(read_all.data["updated"], 1)
        second.refresh_from_db()
        self.assertIsNotNone(second.read_at)

        again = self.client.post("/api/v1/notifications/read-all")
        self.assertEqual(again.data["updated"], 0)

    def test_mark_read_other_students_notification_404(self):
        other_user = User.objects.create_user(email="other@gim.ac.in", password="x")
        other = Student.objects.create(
            user=other_user,
            institute=self.institute,
            name="Other",
            gender=Gender.MALE,
            home_hostel=self.boys,
        )
        note = Notification.objects.create(
            student=other,
            title="Not yours",
            body="Nope",
            type=NotificationType.STRIKE,
            kind=NotificationKind.DANGER,
        )
        response = self.client.post(f"/api/v1/notifications/{note.id}/read")
        self.assertEqual(response.status_code, 404)

    def test_notification_preferences_get_put(self):
        got = self.client.get("/api/v1/notifications/preferences")
        self.assertEqual(got.status_code, 200, got.data)
        self.assertTrue(got.data["bookingConfirmed"])
        self.assertTrue(got.data["strike"])

        updated = self.client.put(
            "/api/v1/notifications/preferences",
            {
                "bookingConfirmed": False,
                "slotReminder": True,
                "exchangeRequest": False,
                "suspension": False,
            },
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertFalse(updated.data["bookingConfirmed"])
        self.assertTrue(updated.data["slotReminder"])
        self.assertFalse(updated.data["exchangeRequest"])
        self.assertTrue(updated.data["ticketUpdate"])
        self.assertFalse(updated.data["suspension"])

    def test_quota_status_uses_rules_engine_bookings(self):
        now = aware(2026, 8, 9, 12)
        self._add_washer(aware(2026, 8, 7, 18))
        self._add_washer(aware(2026, 8, 8, 10))
        # Outside the 7-day window
        self._add_washer(aware(2026, 7, 30, 11))
        # Free cancel does not count
        self._add_washer(aware(2026, 8, 8, 16), counts=False)

        status = quota_status(self.student, now=now, rules=self.rules)
        self.assertEqual(status["used"], 2)
        self.assertEqual(status["limit"], 3)
        self.assertEqual(status["window_days"], 7)
        self.assertEqual(status["resets_at"], aware(2026, 8, 10, 0))

        self._add_washer(aware(2026, 8, 9, 8))
        clears = cooldown_clears_at(self.student, now=now, rules=self.rules)
        self.assertIsNone(clears)
