"""Tests for Track E admin booking grid, cancel, CSV, and analytics."""

from datetime import datetime, time, timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from base.models import BaseUser
from laundry.models import (
    Administrator,
    AvailabilityMiss,
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


class AdminBookingsAnalyticsTests(APITestCase):
    def setUp(self):
        self.institute = Institute.objects.create(
            name="GIM",
            allowed_email_domains=["gim.ac.in"],
        )
        InstituteRule.objects.create(institute=self.institute)
        self.other_institute = Institute.objects.create(
            name="Other",
            allowed_email_domains=["other.edu"],
        )
        self.hostel = Hostel.objects.create(
            institute=self.institute, name="Boys 1", gender=Gender.MALE
        )
        self.other_hostel = Hostel.objects.create(
            institute=self.other_institute, name="Other H", gender=Gender.MALE
        )
        self.machine = Machine.objects.create(
            hostel=self.hostel,
            kind=MachineKind.WASHER,
            location_name="3rd Floor · A Wing",
            operating_window_start=time(6, 0),
            operating_window_end=time(23, 0),
        )
        self.other_machine = Machine.objects.create(
            hostel=self.other_hostel,
            kind=MachineKind.WASHER,
            location_name="Other Washer",
            operating_window_start=time(6, 0),
            operating_window_end=time(23, 0),
        )

        self.admin_user = BaseUser.objects.create_user(
            email="committee@gim.ac.in", password="x"
        )
        Administrator.objects.create(
            user=self.admin_user,
            institute=self.institute,
            display_name="Committee",
        )

        self.student_user = BaseUser.objects.create_user(
            email="stu@gim.ac.in", password="x"
        )
        self.student = Student.objects.create(
            user=self.student_user,
            institute=self.institute,
            name="Ada Lovelace",
            gender=Gender.MALE,
            home_hostel=self.hostel,
        )

        other_stu_user = BaseUser.objects.create_user(
            email="other@other.edu", password="x"
        )
        self.other_student = Student.objects.create(
            user=other_stu_user,
            institute=self.other_institute,
            name="Other Stu",
            gender=Gender.MALE,
            home_hostel=self.other_hostel,
        )

        self.today = timezone.localdate()
        tz = timezone.get_current_timezone()
        self.slot_start = timezone.make_aware(
            datetime.combine(self.today, time(10, 0)), tz
        )
        self.slot_end = self.slot_start + timedelta(hours=1)
        self.booking = Booking.objects.create(
            student=self.student,
            machine=self.machine,
            starts_at=self.slot_start,
            ends_at=self.slot_end,
            channel=BookingChannel.IOS,
        )
        Booking.objects.create(
            student=self.other_student,
            machine=self.other_machine,
            starts_at=self.slot_start,
            ends_at=self.slot_end,
            channel=BookingChannel.WHATSAPP,
        )
        AvailabilityMiss.objects.create(
            student=self.student,
            machine=self.machine,
            date=self.today,
            hour=10,
        )

    def test_grid_requires_admin(self):
        res = self.client.get("/api/v1/admin/bookings/grid")
        self.assertEqual(res.status_code, 401)

        self.client.force_authenticate(self.student_user)
        res = self.client.get("/api/v1/admin/bookings/grid")
        self.assertEqual(res.status_code, 403)

    def test_booking_grid_shapes_cells(self):
        self.client.force_authenticate(self.admin_user)
        res = self.client.get(
            "/api/v1/admin/bookings/grid",
            {"date": self.today.isoformat(), "hostel": str(self.hostel.id)},
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsInstance(res.data, list)
        self.assertGreater(len(res.data), 0)

        # Hours 6–23 for one machine → 18 cells.
        self.assertEqual(len(res.data), 18)
        booked = [c for c in res.data if c["hour"] == 10 and c["student_name"]]
        self.assertEqual(len(booked), 1)
        cell = booked[0]
        self.assertEqual(cell["machine_id"], str(self.machine.id))
        self.assertEqual(cell["student_name"], "Ada Lovelace")
        self.assertEqual(cell["student_id"], str(self.student.id))
        self.assertEqual(cell["channel"]["name"], "iOS app")
        self.assertIn(cell["state"], ("upcoming", "running", "completed"))
        self.assertEqual(cell["date"], self.today.isoformat())

        # Outside window / early closed example: hour 5 is not in grid.
        hours = {c["hour"] for c in res.data}
        self.assertEqual(hours, set(range(6, 24)))

        # Other institute booking must not appear.
        names = {c["student_name"] for c in res.data}
        self.assertNotIn("Other Stu", names)

    def test_booking_detail_and_cancel(self):
        self.client.force_authenticate(self.admin_user)
        # Ensure booking is in the future so cancel is allowed.
        future_start = timezone.now() + timedelta(days=1)
        future_start = future_start.replace(minute=0, second=0, microsecond=0)
        self.booking.starts_at = future_start
        self.booking.ends_at = future_start + timedelta(hours=1)
        self.booking.save(update_fields=["starts_at", "ends_at", "updated_at"])

        res = self.client.get(f"/api/v1/admin/bookings/{self.booking.id}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], str(self.booking.id))
        self.assertEqual(res.data["student_name"], "Ada Lovelace")
        self.assertEqual(res.data["channel"]["name"], "iOS app")
        self.assertIsNone(res.data["cancelled_at"])
        self.assertIn("booked_at_label", res.data)

        res = self.client.post(f"/api/v1/admin/bookings/{self.booking.id}/cancel/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNotNone(res.data["cancelled_at"])
        self.booking.refresh_from_db()
        self.assertIsNotNone(self.booking.cancelled_at)
        self.assertFalse(self.booking.counts_against_quota)

        # Other institute booking is 404 for this admin.
        other = Booking.objects.filter(machine=self.other_machine).first()
        res = self.client.get(f"/api/v1/admin/bookings/{other.id}/")
        self.assertEqual(res.status_code, 404)

    def test_export_csv(self):
        self.client.force_authenticate(self.admin_user)
        res = self.client.get(
            "/api/v1/admin/bookings/export.csv",
            {"date": self.today.isoformat(), "hostel": str(self.hostel.id)},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res["Content-Type"])
        body = res.content.decode("utf-8")
        self.assertIn("Ada Lovelace", body)
        self.assertIn("iOS app", body)
        self.assertNotIn("Other Stu", body)

    def test_analytics_endpoints(self):
        self.client.force_authenticate(self.admin_user)

        res = self.client.get(
            "/api/v1/admin/analytics/demand-by-hour",
            {"hostel": str(self.hostel.id)},
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data), 24)
        hour10 = next(p for p in res.data if p["hour"] == 10)
        self.assertGreaterEqual(hour10["booked"], 1)
        self.assertGreaterEqual(hour10["turned_away"], 1)

        res = self.client.get(
            "/api/v1/admin/analytics/weekday-shape",
            {"hostel": str(self.hostel.id)},
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data), 7)
        labels = [p["label"] for p in res.data]
        self.assertEqual(labels, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        self.assertGreaterEqual(sum(p["booked"] for p in res.data), 1)

        res = self.client.get("/api/v1/admin/analytics/channel-shares")
        self.assertEqual(res.status_code, 200, res.data)
        by_name = {p["name"]: p for p in res.data}
        # One bucket per channel display name, always present so the chart keeps
        # a stable set of slices. Adding a BookingChannel adds a bucket here;
        # tests/test_booking_channel.py guards that none is ever left out.
        self.assertEqual(
            set(by_name),
            {"Android app", "iOS app", "WhatsApp", "Website", "Assistant (MCP)"},
        )
        self.assertGreater(by_name["iOS app"]["pct"], 0)
        self.assertEqual(sum(p["pct"] for p in res.data), 100)
