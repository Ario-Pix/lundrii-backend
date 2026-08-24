"""Smoke tests for Wave 2b admin CRUD APIs."""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from base.models import BaseUser
from laundry.models import (
    Administrator,
    Booking,
    Gender,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    MachineKind,
    Notification,
    NotificationType,
    Student,
    SuperAdministrator,
    Ticket,
    TicketKind,
    TicketStatus,
)


class AdminApiTests(APITestCase):
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
            institute=self.institute, name="Boys 1"
        )
        self.other_hostel = Hostel.objects.create(
            institute=self.other_institute, name="Other H"
        )
        self.machine = Machine.objects.create(
            hostel=self.hostel,
            kind=MachineKind.WASHER,
            location_name="3rd Floor · A Wing",
        )

        self.super_user = BaseUser.objects.create_user(
            email="super@lundrii.app", password="x"
        )
        SuperAdministrator.objects.create(user=self.super_user, display_name="Platform")

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
            name="Ada",
            gender=Gender.MALE,
            home_hostel=self.hostel,
        )

    def test_unauthenticated_rejected(self):
        res = self.client.get("/api/v1/admin/institutes/")
        self.assertEqual(res.status_code, 401)

    def test_student_forbidden(self):
        self.client.force_authenticate(self.student_user)
        res = self.client.get("/api/v1/admin/hostels/")
        self.assertEqual(res.status_code, 403)

    def test_super_admin_institute_crud(self):
        self.client.force_authenticate(self.super_user)
        res = self.client.post(
            "/api/v1/admin/institutes/",
            {"name": "New Inst", "allowed_email_domains": ["new.edu"]},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        inst_id = res.data["id"]
        self.assertTrue(InstituteRule.objects.filter(institute_id=inst_id).exists())
        res = self.client.get("/api/v1/admin/institutes/")
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(res.data["count"], 3)

    def test_admin_hostel_scoped(self):
        self.client.force_authenticate(self.admin_user)
        res = self.client.get("/api/v1/admin/hostels/")
        self.assertEqual(res.status_code, 200)
        rows_by_id = {row["id"]: row for row in res.data["results"]}
        ids = set(rows_by_id)
        self.assertIn(str(self.hostel.id), ids)
        self.assertNotIn(str(self.other_hostel.id), ids)
        self.assertEqual(rows_by_id[str(self.hostel.id)]["machine_count"], 1)
        self.assertEqual(rows_by_id[str(self.hostel.id)]["resident_count"], 1)

        res = self.client.get(f"/api/v1/admin/hostels/{self.hostel.id}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["machine_count"], 1)
        self.assertEqual(res.data["resident_count"], 1)

        res = self.client.post(
            "/api/v1/admin/hostels/",
            {"name": "Boys 2"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(str(res.data["institute"]), str(self.institute.id))

    def test_admin_cannot_create_institute(self):
        self.client.force_authenticate(self.admin_user)
        res = self.client.post(
            "/api/v1/admin/institutes/",
            {"name": "Nope", "allowed_email_domains": ["nope.edu"]},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_machine_offline_cancels_future_bookings(self):
        future = timezone.now() + timedelta(days=1)
        booking = Booking.objects.create(
            student=self.student,
            machine=self.machine,
            starts_at=future,
            ends_at=future + timedelta(hours=1),
        )
        past = timezone.now() - timedelta(days=1)
        past_booking = Booking.objects.create(
            student=self.student,
            machine=self.machine,
            starts_at=past,
            ends_at=past + timedelta(hours=1),
        )

        self.client.force_authenticate(self.admin_user)
        res = self.client.post(
            f"/api/v1/admin/machines/{self.machine.id}/offline/",
            {"is_offline": True},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["is_offline"])
        self.assertEqual(res.data["cancelled_bookings"], 1)

        booking.refresh_from_db()
        past_booking.refresh_from_db()
        self.assertIsNotNone(booking.cancelled_at)
        self.assertFalse(booking.counts_against_quota)
        self.assertIsNone(past_booking.cancelled_at)
        self.assertTrue(
            Notification.objects.filter(
                student=self.student,
                type=NotificationType.BOOKING_CANCELLED_OFFLINE,
                related_object_id=booking.id,
            ).exists()
        )

    def test_student_assign_suspend_strike(self):
        self.client.force_authenticate(self.admin_user)
        res = self.client.post(
            f"/api/v1/admin/students/{self.student.id}/assign/",
            {"gender": "male", "home_hostel": str(self.hostel.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        ends = timezone.now() + timedelta(days=14)
        res = self.client.post(
            f"/api/v1/admin/students/{self.student.id}/suspend/",
            {"suspension_ends": ends.isoformat(), "suspension_reason": "Abuse"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["is_suspended"])

        res = self.client.post(
            f"/api/v1/admin/students/{self.student.id}/strikes/",
            {"reason": "Used someone else's slot"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

    def test_ticket_status_update(self):
        ticket = Ticket.objects.create(
            student=self.student,
            kind=TicketKind.MAINTENANCE,
            machine=self.machine,
            student_note="Not spinning",
        )
        self.client.force_authenticate(self.admin_user)
        res = self.client.patch(
            f"/api/v1/admin/tickets/{ticket.id}/",
            {"status": TicketStatus.RESOLVED, "committee_note": "Fixed."},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], TicketStatus.RESOLVED)
        self.assertEqual(res.data["committee_note"], "Fixed.")
        ticket.refresh_from_db()
        self.assertIsNotNone(ticket.resolved_at)
        self.assertEqual(ticket.events.count(), 1)

    def test_admin_profile_get_and_patch(self):
        self.client.force_authenticate(self.admin_user)
        res = self.client.get("/api/v1/admin/me/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["email"], "committee@gim.ac.in")
        self.assertEqual(res.data["role_label"], "Administrator")
        self.assertEqual(str(res.data["institute_id"]), str(self.institute.id))

        res = self.client.patch(
            "/api/v1/admin/profile/",
            {"display_name": "New Committee"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["display_name"], "New Committee")
        self.admin_user.administrator.refresh_from_db()
        self.assertEqual(self.admin_user.administrator.display_name, "New Committee")

    def test_machine_hours_and_online(self):
        self.machine.is_offline = True
        self.machine.save(update_fields=["is_offline", "updated_at"])
        self.client.force_authenticate(self.admin_user)

        res = self.client.patch(
            f"/api/v1/admin/machines/{self.machine.id}/hours/",
            {
                "operating_window_start": "06:00:00",
                "operating_window_end": "22:00:00",
                "slot_length_minutes": 45,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["operating_window_start"], "06:00:00")
        self.assertEqual(res.data["operating_window_end"], "22:00:00")
        self.assertEqual(res.data["slot_length_minutes"], 45)

        res = self.client.post(f"/api/v1/admin/machines/{self.machine.id}/online/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["is_offline"])

    def test_create_student_and_list_aliases(self):
        self.client.force_authenticate(self.admin_user)
        res = self.client.post(
            "/api/v1/admin/students/",
            {
                "name": "Bea",
                "email": "bea@gim.ac.in",
                "phone": "999",
                "gender": "male",
                "hostel": str(self.hostel.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["email"], "bea@gim.ac.in")
        self.assertEqual(str(res.data["home_hostel"]), str(self.hostel.id))

        res = self.client.get(
            "/api/v1/admin/students/",
            {"hostelId": str(self.hostel.id), "status": "active", "q": "Bea"},
        )
        self.assertEqual(res.status_code, 200)
        emails = {row["email"] for row in res.data["results"]}
        self.assertIn("bea@gim.ac.in", emails)

    def test_student_booking_history(self):
        future = timezone.now() + timedelta(days=1)
        Booking.objects.create(
            student=self.student,
            machine=self.machine,
            starts_at=future,
            ends_at=future + timedelta(hours=1),
        )
        self.client.force_authenticate(self.admin_user)
        res = self.client.get(f"/api/v1/admin/students/{self.student.id}/bookings/")
        self.assertEqual(res.status_code, 200, res.data)
        rows = res.data["results"] if isinstance(res.data, dict) and "results" in res.data else res.data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "Upcoming")
        self.assertEqual(rows[0]["machine"], self.machine.location_name)

    def test_revoke_strike_and_suspensions(self):
        self.client.force_authenticate(self.admin_user)
        res = self.client.post(
            f"/api/v1/admin/students/{self.student.id}/strikes/",
            {"reason": "Left laundry overnight"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        strike_id = res.data["id"]

        res = self.client.post(f"/api/v1/admin/strikes/{strike_id}/revoke/")
        self.assertEqual(res.status_code, 204)

        from laundry.models import Strike

        self.assertFalse(Strike.objects.get(pk=strike_id).is_active)

        ends = timezone.now() + timedelta(days=7)
        self.client.post(
            f"/api/v1/admin/students/{self.student.id}/suspend/",
            {"suspension_ends": ends.isoformat(), "suspension_reason": "Abuse"},
            format="json",
        )
        res = self.client.get("/api/v1/admin/suspensions/")
        self.assertEqual(res.status_code, 200, res.data)
        ids = {row["student_id"] for row in res.data}
        self.assertIn(str(self.student.id), ids)

    def test_institute_domains_and_rules_write_through(self):
        self.client.force_authenticate(self.admin_user)
        res = self.client.patch(
            f"/api/v1/admin/institutes/{self.institute.id}/",
            {"allowed_email_domains": ["gim.ac.in", "student.gim.ac.in"]},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data["allowed_email_domains"],
            ["gim.ac.in", "student.gim.ac.in"],
        )

        rule = self.institute.rules
        res = self.client.patch(
            f"/api/v1/admin/rules/{rule.id}/",
            {"allowed_email_domains": ["gim.ac.in"], "quota_limit": 4},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["quota_limit"], 4)
        self.institute.refresh_from_db()
        self.assertEqual(self.institute.allowed_email_domains, ["gim.ac.in"])

    @patch("base.tasks.send_password_reset_email_with_token", return_value=True)
    def test_promote_and_send_reset_link(self, mock_send):
        self.client.force_authenticate(self.admin_user)
        res = self.client.post(f"/api/v1/admin/students/{self.student.id}/promote/")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(Administrator.objects.filter(user=self.student_user).exists())

        res = self.client.post(
            f"/api/v1/admin/students/{self.student.id}/send-reset-link/"
        )
        self.assertEqual(res.status_code, 200, res.data)
        mock_send.assert_called_once()

    def test_students_import_csv(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        csv_body = (
            "name,email,phone,hostel,gender\n"
            "Nikhil,nikhil@gim.ac.in,111,Boys 1,male\n"
            "Ada,stu@gim.ac.in,222,Boys 1,male\n"
            "Bad,bad@gmail.com,333,Boys 1,male\n"
        )
        upload = SimpleUploadedFile(
            "roster.csv", csv_body.encode("utf-8"), content_type="text/csv"
        )
        self.client.force_authenticate(self.admin_user)
        res = self.client.post(
            "/api/v1/admin/students/import/",
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["created"], 1)
        self.assertEqual(res.data["skipped"], 1)
        self.assertEqual(res.data["errors"], 1)
        self.assertTrue(
            Student.objects.filter(user__email="nikhil@gim.ac.in").exists()
        )
