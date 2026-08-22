"""Focused tests for exchange request / swap + approve-time rule checks."""

from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from laundry.models import (
    ExchangeKind,
    ExchangeStatus,
    Gender,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    MachineKind,
    Notification,
    NotificationType,
    Student,
)
from laundry.services.exchanges import (
    approve_exchange,
    create_exchange,
    exchanges_qs,
    expire_stale_pendings,
)

User = get_user_model()


def aware(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute))


class ExchangeFixtureMixin:
    def make_world(self, **rule_kwargs):
        self.institute = Institute.objects.create(
            name="GIM Exchange Test",
            allowed_email_domains=["gim.ac.in"],
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
        self.now = aware(2026, 8, 9, 10, 0)
        self.holder = self.make_student("rohan@gim.ac.in", "Rohan Shetty", Gender.MALE)
        self.requester = self.make_student("priya@gim.ac.in", "Priya Kulkarni", Gender.MALE)

    def make_student(self, email, name, gender, *, verified=True, suspended_until=None):
        user = User.objects.create_user(email=email, password="unused")
        return Student.objects.create(
            user=user,
            institute=self.institute,
            name=name,
            gender=gender,
            home_hostel=self.boys,
            email_verified_at=self.now if verified else None,
            suspension_ends=suspended_until,
        )

    def add_booking(self, student, machine, starts_at, *, counts=True):
        from laundry.models import Booking

        return Booking.objects.create(
            student=student,
            machine=machine,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            counts_against_quota=counts,
        )


class ExchangeServiceTests(ExchangeFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_request_transfers_slot_and_counts_against_requester(self):
        target = self.add_booking(self.holder, self.washer, aware(2026, 8, 9, 16))
        exchange = create_exchange(
            self.requester,
            kind=ExchangeKind.REQUEST,
            target_booking_id=target.id,
            now=self.now,
        )
        self.assertEqual(exchange.status, ExchangeStatus.PENDING)
        self.assertTrue(
            Notification.objects.filter(
                student=self.holder,
                type=NotificationType.EXCHANGE_REQUEST,
            ).exists()
        )

        result = approve_exchange(self.holder, exchange, now=self.now)
        self.assertEqual(result.status, ExchangeStatus.APPROVED)
        target.refresh_from_db()
        self.assertEqual(target.student_id, self.requester.pk)
        self.assertTrue(target.counts_against_quota)
        self.assertTrue(
            Notification.objects.filter(
                student=self.requester,
                type=NotificationType.EXCHANGE_OUTCOME,
            ).exists()
        )

    def test_swap_exchanges_holders_quota_unchanged(self):
        target = self.add_booking(self.holder, self.washer, aware(2026, 8, 9, 16), counts=True)
        offered = self.add_booking(
            self.requester, self.washer_b, aware(2026, 8, 9, 18), counts=True
        )
        exchange = create_exchange(
            self.requester,
            kind=ExchangeKind.SWAP,
            target_booking_id=target.id,
            offered_booking_id=offered.id,
            now=self.now,
        )
        result = approve_exchange(self.holder, exchange, now=self.now)
        self.assertEqual(result.status, ExchangeStatus.APPROVED)
        target.refresh_from_db()
        offered.refresh_from_db()
        self.assertEqual(target.student_id, self.requester.pk)
        self.assertEqual(offered.student_id, self.holder.pk)
        self.assertTrue(target.counts_against_quota)
        self.assertTrue(offered.counts_against_quota)

    def test_create_skips_quota_approve_fails(self):
        self.rules.quota_limit = 1
        self.rules.save(update_fields=["quota_limit"])
        self.add_booking(self.requester, self.washer_b, aware(2026, 8, 9, 14))
        target = self.add_booking(self.holder, self.washer, aware(2026, 8, 9, 18))

        exchange = create_exchange(
            self.requester,
            kind=ExchangeKind.REQUEST,
            target_booking_id=target.id,
            now=self.now,
        )
        self.assertEqual(exchange.status, ExchangeStatus.PENDING)

        result = approve_exchange(self.holder, exchange, now=self.now)
        self.assertEqual(result.status, ExchangeStatus.FAILED)
        self.assertIn("quota", result.failure_reason.lower())
        target.refresh_from_db()
        self.assertEqual(target.student_id, self.holder.pk)
        self.assertEqual(
            Notification.objects.filter(
                type=NotificationType.EXCHANGE_OUTCOME,
                related_object_id=result.id,
            ).count(),
            2,
        )

    def test_pending_expires_when_slot_starts(self):
        target = self.add_booking(self.holder, self.washer, aware(2026, 8, 9, 16))
        exchange = create_exchange(
            self.requester,
            kind=ExchangeKind.REQUEST,
            target_booking_id=target.id,
            now=self.now,
        )
        expire_stale_pendings(now=aware(2026, 8, 9, 16, 0))
        exchange.refresh_from_db()
        self.assertEqual(exchange.status, ExchangeStatus.EXPIRED)

    def test_pending_expires_when_target_cancelled(self):
        target = self.add_booking(self.holder, self.washer, aware(2026, 8, 9, 16))
        exchange = create_exchange(
            self.requester,
            kind=ExchangeKind.REQUEST,
            target_booking_id=target.id,
            now=self.now,
        )
        target.cancelled_at = self.now
        target.save(update_fields=["cancelled_at", "updated_at"])
        list(exchanges_qs(self.holder, direction="incoming"))
        exchange.refresh_from_db()
        self.assertEqual(exchange.status, ExchangeStatus.EXPIRED)


class ExchangeAPITests(ExchangeFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.holder_client = APIClient()
        self.requester_client = APIClient()
        self.holder_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.holder.user).access_token}"
        )
        self.requester_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.requester.user).access_token}"
        )

    def _future_start(self, hours=8):
        return (timezone.localtime() + timedelta(hours=hours)).replace(
            minute=0, second=0, microsecond=0
        )

    def test_create_list_approve_request_api(self):
        start = self._future_start(10)
        target = self.add_booking(self.holder, self.washer, start)
        response = self.requester_client.post(
            "/api/v1/exchanges",
            {"kind": "request", "targetBookingId": str(target.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        exchange_id = response.data["id"]
        self.assertEqual(response.data["status"], "pending")
        self.assertEqual(response.data["kind"], "request")
        self.assertIsNone(response.data["offeredBooking"])

        incoming = self.holder_client.get("/api/v1/exchanges", {"direction": "incoming"})
        self.assertEqual(incoming.status_code, 200, incoming.data)
        self.assertEqual(incoming.data["count"], 1)
        self.assertEqual(incoming.data["results"][0]["direction"], "incoming")

        outgoing = self.requester_client.get(
            "/api/v1/exchanges", {"direction": "outgoing"}
        )
        self.assertEqual(outgoing.data["count"], 1)

        approved = self.holder_client.post(f"/api/v1/exchanges/{exchange_id}/approve")
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["status"], "approved")
        target.refresh_from_db()
        self.assertEqual(target.student_id, self.requester.pk)

    def test_swap_and_approve_fail_api(self):
        self.rules.quota_limit = 1
        self.rules.save(update_fields=["quota_limit"])
        self.add_booking(self.requester, self.washer_b, self._future_start(6))
        target = self.add_booking(self.holder, self.washer, self._future_start(12))
        offered = self.add_booking(self.requester, self.washer_b, self._future_start(18))

        created = self.requester_client.post(
            "/api/v1/exchanges",
            {
                "kind": "swap",
                "target_booking_id": str(target.id),
                "offered_booking_id": str(offered.id),
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        exchange_id = created.data["id"]

        failed = self.holder_client.post(f"/api/v1/exchanges/{exchange_id}/approve")
        self.assertEqual(failed.status_code, 200, failed.data)
        self.assertEqual(failed.data["status"], "failed")
        self.assertTrue(failed.data["failureReason"])
        target.refresh_from_db()
        offered.refresh_from_db()
        self.assertEqual(target.student_id, self.holder.pk)
        self.assertEqual(offered.student_id, self.requester.pk)

    def test_reject_and_withdraw(self):
        target = self.add_booking(self.holder, self.washer, self._future_start(10))
        created = self.requester_client.post(
            "/api/v1/exchanges",
            {"kind": "request", "targetBookingId": str(target.id)},
            format="json",
        )
        exchange_id = created.data["id"]
        rejected = self.holder_client.post(
            f"/api/v1/exchanges/{exchange_id}/reject",
            {"note": "Cannot swap this week."},
            format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertEqual(rejected.data["status"], "rejected")
        self.assertEqual(rejected.data["rejectNote"], "Cannot swap this week.")

        target_b = self.add_booking(self.holder, self.washer, self._future_start(14))
        created_b = self.requester_client.post(
            "/api/v1/exchanges",
            {"kind": "request", "targetBookingId": str(target_b.id)},
            format="json",
        )
        withdrawn = self.requester_client.post(
            f"/api/v1/exchanges/{created_b.data['id']}/withdraw"
        )
        self.assertEqual(withdrawn.status_code, 200, withdrawn.data)
        self.assertEqual(withdrawn.data["status"], "rejected")
        self.assertIn("Withdrawn", withdrawn.data["failureReason"])
