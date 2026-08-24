"""Focused tests for student tickets and suspension enforcement (Track B)."""

from datetime import datetime, time, timedelta
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

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
    Strike,
    Student,
    Ticket,
    TicketKind,
    TicketStatus,
)
from laundry.permissions import IsStudentCanMutate
from laundry.services.access import assert_can_mutate

User = get_user_model()


def aware(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute))


class StudentTicketApiTests(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(
            name="GIM Tickets",
            allowed_email_domains=["gim.ac.in"],
        )
        InstituteRule.objects.create(institute=self.institute)
        self.boys = Hostel.objects.create(
            institute=self.institute, name="Boys 1"
        )
        self.girls = Hostel.objects.create(
            institute=self.institute, name="Girls 1"
        )
        self.washer = Machine.objects.create(
            hostel=self.boys,
            kind=MachineKind.WASHER,
            location_name="3rd Floor · A Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.girl_washer = Machine.objects.create(
            hostel=self.girls,
            kind=MachineKind.WASHER,
            location_name="Girls Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.now = timezone.now()
        self.student = self._make_student("aarav@gim.ac.in", "Aarav Mehta", Gender.MALE)
        self.peer = self._make_student("peer@gim.ac.in", "Peer Shah", Gender.MALE)
        self.admin_user = User.objects.create_user(
            email="committee@gim.ac.in", password="x"
        )
        Administrator.objects.create(
            user=self.admin_user,
            institute=self.institute,
            display_name="Committee",
        )
        self.client = APIClient()
        token = RefreshToken.for_user(self.student.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def _make_student(self, email, name, gender, *, verified=True, suspended_until=None):
        user = User.objects.create_user(email=email, password="unused")
        return Student.objects.create(
            user=user,
            institute=self.institute,
            name=name,
            gender=gender,
            home_hostel=self.boys if gender == Gender.MALE else self.girls,
            email_verified_at=self.now if verified else None,
            suspension_ends=suspended_until,
        )

    def _add_booking(self, student, machine, starts_at):
        return Booking.objects.create(
            student=student,
            machine=machine,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
        )

    def test_unauthenticated_rejected(self):
        anon = APIClient()
        res = anon.get("/api/v1/tickets")
        self.assertEqual(res.status_code, 401)

    def test_raise_maintenance_creates_notification_no_thread(self):
        res = self.client.post(
            "/api/v1/tickets",
            {
                "kind": TicketKind.MAINTENANCE,
                "note": "Drum stopped mid-cycle.",
                "machineId": str(self.washer.id),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["kind"], TicketKind.MAINTENANCE)
        self.assertEqual(res.data["status"], TicketStatus.OPEN)
        self.assertEqual(res.data["note"], "Drum stopped mid-cycle.")
        self.assertEqual(res.data["studentNote"], "Drum stopped mid-cycle.")
        self.assertEqual(res.data["photoUrl"], "")
        self.assertIsNone(res.data["committeeNote"])
        self.assertEqual(str(res.data["machineId"]), str(self.washer.id))
        self.assertEqual(res.data["machineName"], "3rd Floor · A Wing")
        self.assertIsNotNone(res.data["number"])
        self.assertNotIn("steps", res.data)

        ticket = Ticket.objects.get(pk=res.data["id"])
        self.assertEqual(ticket.events.count(), 1)
        self.assertTrue(
            Notification.objects.filter(
                student=self.student,
                type=NotificationType.TICKET_UPDATE,
                related_object_id=ticket.id,
            ).exists()
        )

    def test_note_required(self):
        res = self.client.post(
            "/api/v1/tickets",
            {
                "kind": TicketKind.MAINTENANCE,
                "machineId": str(self.washer.id),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "VALIDATION_ERROR")

    def test_conflict_kind_is_rejected(self):
        start = aware(2026, 8, 9, 13)
        booking = self._add_booking(self.student, self.washer, start)
        res = self.client.post(
            "/api/v1/tickets",
            {
                "kind": TicketKind.CONFLICT,
                "note": "Someone else was using the machine.",
                "machineId": str(self.washer.id),
                "bookingId": str(booking.id),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Ticket.objects.filter(kind=TicketKind.CONFLICT).exists())

    def test_maintenance_requires_machine(self):
        res = self.client.post(
            "/api/v1/tickets",
            {"kind": TicketKind.MAINTENANCE, "note": "Broken."},
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "VALIDATION_ERROR")

    def test_other_institute_machine_hidden(self):
        other = Institute.objects.create(
            name="Other Campus",
            allowed_email_domains=["other.edu"],
        )
        other_hostel = Hostel.objects.create(institute=other, name="Other H")
        other_washer = Machine.objects.create(
            hostel=other_hostel,
            kind=MachineKind.WASHER,
            location_name="1st Floor",
            operating_window_start=self.washer.operating_window_start,
            operating_window_end=self.washer.operating_window_end,
            slot_length_minutes=60,
        )
        res = self.client.post(
            "/api/v1/tickets",
            {
                "kind": TicketKind.MAINTENANCE,
                "note": "Broken.",
                "machineId": str(other_washer.id),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 404)

    @override_settings(
        CLOUDINARY_URL="",
        CLOUDINARY_CLOUD_NAME="",
        CLOUDINARY_API_KEY="",
        CLOUDINARY_API_SECRET="",
    )
    def test_photo_without_cloudinary_returns_not_configured(self):
        photo = SimpleUploadedFile(
            "leak.jpg",
            b"\xff\xd8\xff\xe0fakejpeg",
            content_type="image/jpeg",
        )
        res = self.client.post(
            "/api/v1/tickets",
            {
                "kind": TicketKind.MAINTENANCE,
                "note": "Leaking with photo.",
                "machineId": str(self.washer.id),
                "photo": photo,
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data["code"], "CLOUDINARY_NOT_CONFIGURED")
        self.assertIn("Cloudinary is not configured", res.data["detail"])
        self.assertNotIn("ErrorDetail", res.data["detail"])

    @patch(
        "laundry.services.tickets.upload_ticket_photo",
        return_value="https://res.cloudinary.com/lundrii/image/upload/v1/tickets/abc.png",
    )
    def test_photo_upload_sets_photo_url(self, mock_upload):
        photo = SimpleUploadedFile(
            "drum.png",
            BytesIO(b"pngbytes").getvalue(),
            content_type="image/png",
        )
        res = self.client.post(
            "/api/v1/tickets",
            {
                "kind": TicketKind.MAINTENANCE,
                "note": "Photo attached.",
                "machineId": str(self.washer.id),
                "photo": photo,
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            res.data["photoUrl"],
            "https://res.cloudinary.com/lundrii/image/upload/v1/tickets/abc.png",
        )
        mock_upload.assert_called_once()
        ticket = Ticket.objects.get(pk=res.data["id"])
        self.assertEqual(ticket.photo_url, res.data["photoUrl"])

    def test_list_and_detail_without_thread_admin_can_resolve(self):
        raise_res = self.client.post(
            "/api/v1/tickets",
            {
                "kind": TicketKind.MAINTENANCE,
                "note": "Not spinning.",
                "machineId": str(self.washer.id),
            },
            format="multipart",
        )
        self.assertEqual(raise_res.status_code, 201, raise_res.data)
        ticket_id = raise_res.data["id"]

        detail = self.client.get(f"/api/v1/tickets/{ticket_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["note"], "Not spinning.")
        self.assertIn("photoUrl", detail.data)
        self.assertIn("createdAt", detail.data)
        self.assertNotIn("steps", detail.data)

        admin_client = APIClient()
        admin_client.force_authenticate(self.admin_user)
        patch = admin_client.patch(
            f"/api/v1/admin/tickets/{ticket_id}/",
            {"status": TicketStatus.RESOLVED, "committee_note": "Fixed overnight."},
            format="json",
        )
        self.assertEqual(patch.status_code, 200, patch.data)
        self.assertEqual(patch.data["status"], TicketStatus.RESOLVED)
        self.assertIn("photo_url", patch.data)

        listed = self.client.get("/api/v1/tickets")
        self.assertEqual(listed.status_code, 200)
        rows = listed.data["results"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], TicketStatus.RESOLVED)
        self.assertEqual(row["committeeNote"], "Fixed overnight.")
        self.assertNotIn("steps", row)

        open_only = self.client.get("/api/v1/tickets", {"status": "open"})
        self.assertEqual(open_only.status_code, 200)
        self.assertEqual(open_only.data["count"], 0)

    def test_peer_cannot_see_or_detail_other_tickets(self):
        raise_res = self.client.post(
            "/api/v1/tickets",
            {
                "kind": TicketKind.MAINTENANCE,
                "note": "Mine.",
                "machineId": str(self.washer.id),
            },
            format="multipart",
        )
        ticket_id = raise_res.data["id"]
        peer_client = APIClient()
        token = RefreshToken.for_user(self.peer.user)
        peer_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        res = peer_client.get("/api/v1/tickets")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["count"], 0)
        detail = peer_client.get(f"/api/v1/tickets/{ticket_id}")
        self.assertEqual(detail.status_code, 404)

    def test_suspended_can_raise_and_list_tickets_but_cannot_book(self):
        ends = timezone.now() + timedelta(days=14)
        self.student.suspension_ends = ends
        self.student.suspension_reason = "After ticket #427"
        self.student.save(update_fields=["suspension_ends", "suspension_reason", "updated_at"])

        start = (timezone.localtime() + timedelta(hours=8)).replace(
            minute=0, second=0, microsecond=0
        )
        book = self.client.post(
            "/api/v1/bookings",
            {"machineId": str(self.washer.id), "startsAt": start.isoformat()},
            format="json",
        )
        self.assertEqual(book.status_code, 403)
        self.assertEqual(book.data["code"], "SUSPENDED")
        self.assertIn("clearsAt", book.data)

        raise_res = self.client.post(
            "/api/v1/tickets",
            {
                "kind": TicketKind.MAINTENANCE,
                "note": "Still broken while suspended.",
                "machineId": str(self.washer.id),
            },
            format="multipart",
        )
        self.assertEqual(raise_res.status_code, 201, raise_res.data)

        listed = self.client.get("/api/v1/tickets")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["count"], 1)

    def test_suspended_cannot_create_exchange(self):
        self.student.suspension_ends = timezone.now() + timedelta(days=14)
        self.student.save(
            update_fields=["suspension_ends", "updated_at"]
        )
        res = self.client.post(
            "/api/v1/exchanges",
            {"kind": "request", "targetBookingId": str(self.washer.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "SUSPENDED")

    def test_strike_does_not_block_tickets_or_bookings(self):
        Strike.objects.create(
            student=self.student,
            reason="Left laundry in the drum.",
            date=timezone.localdate(),
            recorded_by=self.admin_user,
        )
        res = self.client.post(
            "/api/v1/tickets",
            {
                "kind": TicketKind.MAINTENANCE,
                "note": "Report after a strike.",
                "machineId": str(self.washer.id),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 201, res.data)

    def test_assert_can_mutate_helper_and_permission_exist(self):
        self.assertTrue(callable(assert_can_mutate))
        self.assertTrue(issubclass(IsStudentCanMutate, object))
        from base.exceptions import APIError

        self.student.suspension_ends = timezone.now() + timedelta(days=1)
        self.student.save(update_fields=["suspension_ends", "updated_at"])
        with self.assertRaises(APIError) as ctx:
            assert_can_mutate(self.student)
        self.assertEqual(ctx.exception.code, "SUSPENDED")
