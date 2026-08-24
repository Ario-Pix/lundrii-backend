"""
The admin-portal capabilities the feature spec asks for and the API lacked:
an action log, dashboard headline numbers, a needs-attention list, an activity
feed, impact previews before destructive changes, and self-service password
change.
"""

from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from laundry.models import (
    AdminAuditLog,
    Administrator,
    Booking,
    Gender,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    MachineKind,
    Student,
    Ticket,
    TicketKind,
    TicketStatus,
)

User = get_user_model()


class AdminOpsWorldMixin:
    password = "LundriiAdmin9!"

    def make_world(self):
        self.institute = Institute.objects.create(
            name="GIM Test", allowed_email_domains=["gim.ac.in"]
        )
        InstituteRule.objects.create(
            institute=self.institute,
            quota_limit=99,
            quota_window_days=7,
            cooldown_hours=0,
            advance_window_days=30,
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
        self.admin_user = User.objects.create_user(
            email="committee@gim.ac.in", password=self.password
        )
        self.admin = Administrator.objects.create(
            user=self.admin_user, institute=self.institute, display_name="Committee"
        )
        self.student = self.make_student("aarav@gim.ac.in", "Aarav Mehta")
        self.day = timezone.localdate() + timedelta(days=2)

        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {RefreshToken.for_user(self.admin_user).access_token}"
            )
        )

    def make_student(self, email, name):
        user = User.objects.create_user(email=email, password="unused")
        return Student.objects.create(
            user=user,
            institute=self.institute,
            name=name,
            gender=Gender.MALE,
            home_hostel=self.hostel,
            email_verified_at=timezone.now(),
        )

    def at(self, hour, minute=0):
        return timezone.make_aware(
            timezone.datetime.combine(self.day, time(hour, minute))
        )

    def add_booking(self, student=None, hour=14):
        return Booking.objects.create(
            student=student or self.student,
            machine=self.washer,
            starts_at=self.at(hour),
            ends_at=self.at(hour + 1),
        )


class AuditLogTests(AdminOpsWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_taking_a_machine_offline_is_logged(self):
        self.add_booking()
        response = self.client.post(
            f"/api/v1/admin/machines/{self.washer.id}/offline/",
            {"is_offline": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        entry = AdminAuditLog.objects.get()
        self.assertEqual(entry.action, AdminAuditLog.Action.MACHINE_OFFLINE)
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.actor_label, "Committee")
        self.assertIn("3rd Floor · A Wing", entry.summary)
        self.assertEqual(entry.metadata["cancelled_bookings"], 1)
        self.assertEqual(entry.metadata["students_notified"], 1)

    def test_disabling_and_enabling_a_student_is_logged(self):
        self.client.post(f"/api/v1/admin/students/{self.student.id}/disable/")
        self.client.post(f"/api/v1/admin/students/{self.student.id}/enable/")
        actions = list(
            AdminAuditLog.objects.order_by("created_at").values_list("action", flat=True)
        )
        self.assertEqual(
            actions,
            [
                AdminAuditLog.Action.STUDENT_DISABLED,
                AdminAuditLog.Action.STUDENT_ENABLED,
            ],
        )

    def test_labels_survive_the_target_being_renamed(self):
        """
        A log that rewrites itself when the world moves on cannot answer
        "what did we do, and when".
        """
        self.client.post(
            f"/api/v1/admin/machines/{self.washer.id}/offline/",
            {"is_offline": True},
            format="json",
        )
        self.washer.location_name = "Renamed Later"
        self.washer.save(update_fields=["location_name"])
        self.admin.display_name = "Someone Else"
        self.admin.save(update_fields=["display_name"])

        entry = AdminAuditLog.objects.get()
        self.assertIn("3rd Floor · A Wing", entry.summary)
        self.assertEqual(entry.actor_label, "Committee")

    def test_log_is_listable_and_filterable(self):
        self.client.post(f"/api/v1/admin/students/{self.student.id}/disable/")
        self.client.post(f"/api/v1/admin/students/{self.student.id}/enable/")

        response = self.client.get("/api/v1/admin/audit-log")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

        filtered = self.client.get(
            "/api/v1/admin/audit-log", {"action": "student.disabled"}
        )
        self.assertEqual(filtered.data["count"], 1)

        by_admin = self.client.get(
            "/api/v1/admin/audit-log", {"administrator": str(self.admin.id)}
        )
        self.assertEqual(by_admin.data["count"], 2)

    def test_date_filters(self):
        self.client.post(f"/api/v1/admin/students/{self.student.id}/disable/")
        today = timezone.localdate().isoformat()
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()

        self.assertEqual(
            self.client.get("/api/v1/admin/audit-log", {"date_from": today}).data["count"],
            1,
        )
        self.assertEqual(
            self.client.get("/api/v1/admin/audit-log", {"date_from": tomorrow}).data[
                "count"
            ],
            0,
        )

    def test_log_is_scoped_to_the_administrators_institute(self):
        other_inst = Institute.objects.create(name="Other", allowed_email_domains=["o.ac.in"])
        AdminAuditLog.objects.create(
            institute=other_inst,
            actor_label="Someone",
            action=AdminAuditLog.Action.MACHINE_OFFLINE,
            summary="Not yours",
        )
        response = self.client.get("/api/v1/admin/audit-log")
        self.assertEqual(response.data["count"], 0)

    def test_log_is_read_only(self):
        self.client.post(f"/api/v1/admin/students/{self.student.id}/disable/")
        entry = AdminAuditLog.objects.get()
        for method, url in (
            ("delete", f"/api/v1/admin/audit-log/{entry.id}"),
            ("patch", f"/api/v1/admin/audit-log/{entry.id}"),
        ):
            with self.subTest(method=method):
                response = getattr(self.client, method)(url)
                self.assertIn(
                    response.status_code,
                    (status.HTTP_404_NOT_FOUND, status.HTTP_405_METHOD_NOT_ALLOWED),
                )
        self.assertEqual(AdminAuditLog.objects.count(), 1)

    def test_a_failing_audit_write_never_breaks_the_action(self):
        import logging
        from unittest.mock import patch

        logging.disable(logging.CRITICAL)  # the failure is logged on purpose
        self.addCleanup(logging.disable, logging.NOTSET)
        with patch(
            "laundry.services.audit.AdminAuditLog.objects.create",
            side_effect=RuntimeError("log storage down"),
        ):
            response = self.client.post(
                f"/api/v1/admin/students/{self.student.id}/disable/"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)


class ImpactPreviewTests(AdminOpsWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_offline_impact_lists_who_would_lose_a_booking(self):
        other = self.make_student("riya@gim.ac.in", "Riya")
        self.add_booking(hour=14)
        self.add_booking(student=other, hour=16)

        response = self.client.get(
            f"/api/v1/admin/machines/{self.washer.id}/offline-impact"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["affected_count"], 2)
        self.assertEqual(response.data["students_notified"], 2)
        emails = {b["student_email"] for b in response.data["bookings"]}
        self.assertEqual(emails, {"aarav@gim.ac.in", "riya@gim.ac.in"})

    def test_preview_does_not_change_anything(self):
        self.add_booking()
        self.client.get(f"/api/v1/admin/machines/{self.washer.id}/offline-impact")
        self.washer.refresh_from_db()
        self.assertFalse(self.washer.is_offline)
        self.assertEqual(Booking.objects.filter(cancelled_at__isnull=True).count(), 1)

    def test_preview_matches_what_the_action_actually_cancels(self):
        self.add_booking(hour=14)
        self.add_booking(hour=16)
        preview = self.client.get(
            f"/api/v1/admin/machines/{self.washer.id}/offline-impact"
        ).data
        acted = self.client.post(
            f"/api/v1/admin/machines/{self.washer.id}/offline/",
            {"is_offline": True},
            format="json",
        ).data
        self.assertEqual(preview["affected_count"], acted["cancelled_bookings"])

    def test_hours_impact_finds_bookings_outside_a_narrower_window(self):
        self.add_booking(hour=2)   # would fall outside 08:00–20:00
        self.add_booking(hour=14)  # stays inside

        response = self.client.post(
            f"/api/v1/admin/machines/{self.washer.id}/hours-impact",
            {"operating_window_start": "08:00", "operating_window_end": "20:00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["affected_count"], 1)
        self.assertEqual(
            timezone.localtime(
                timezone.datetime.fromisoformat(
                    response.data["bookings"][0]["starts_at"].replace("Z", "+00:00")
                )
            ).hour,
            2,
        )


class HoursChangeTests(AdminOpsWorldMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.stranded = self.add_booking(hour=2)
        self.kept = self.add_booking(hour=14)

    def _narrow(self, **extra):
        return self.client.patch(
            f"/api/v1/admin/machines/{self.washer.id}/hours/",
            {
                "operating_window_start": "08:00",
                "operating_window_end": "20:00",
                **extra,
            },
            format="json",
        )

    def test_stranded_bookings_are_kept_by_default(self):
        """Silently destroying a student's booking must take an explicit ask."""
        response = self._narrow()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["stranded_bookings"], 1)
        self.assertEqual(response.data["cancelled_bookings"], 0)
        self.stranded.refresh_from_db()
        self.assertIsNone(self.stranded.cancelled_at)

    def test_stranded_bookings_are_cancelled_when_asked(self):
        response = self._narrow(cancel_outside=True)
        self.assertEqual(response.data["cancelled_bookings"], 1)
        self.stranded.refresh_from_db()
        self.kept.refresh_from_db()
        self.assertIsNotNone(self.stranded.cancelled_at)
        self.assertIsNone(self.kept.cancelled_at)

    def test_forced_cancellation_does_not_punish_the_student(self):
        self._narrow(cancel_outside=True)
        self.stranded.refresh_from_db()
        self.assertFalse(self.stranded.is_late_cancel)
        self.assertFalse(self.stranded.counts_against_quota)

    def test_the_change_is_logged_with_its_impact(self):
        self._narrow(cancel_outside=True)
        entry = AdminAuditLog.objects.get(action=AdminAuditLog.Action.MACHINE_HOURS)
        self.assertEqual(entry.metadata["stranded_bookings"], 1)
        self.assertEqual(entry.metadata["cancelled_bookings"], 1)


class DashboardTests(AdminOpsWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_summary_numbers(self):
        self.add_booking(hour=14)
        self.add_booking(hour=16)
        response = self.client.get(
            "/api/v1/admin/dashboard/summary", {"date": self.day.isoformat()}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["bookings"], 2)
        self.assertEqual(response.data["capacity_slots"], 24)
        self.assertEqual(response.data["capacity_used_pct"], 8)
        self.assertEqual(response.data["machines_total"], 1)
        self.assertEqual(response.data["machines_offline"], 0)
        self.assertEqual(response.data["students_total"], 1)

    def test_offline_machines_do_not_inflate_capacity(self):
        """
        An offline machine offers no slots. Counting it would make utilisation
        look low precisely when capacity is most constrained.
        """
        self.washer.is_offline = True
        self.washer.save(update_fields=["is_offline"])
        response = self.client.get(
            "/api/v1/admin/dashboard/summary", {"date": self.day.isoformat()}
        )
        self.assertEqual(response.data["capacity_slots"], 0)
        self.assertEqual(response.data["capacity_used_pct"], 0)
        self.assertEqual(response.data["machines_offline"], 1)

    def test_attention_flags_open_maintenance(self):
        Ticket.objects.create(
            student=self.student,
            machine=self.washer,
            kind=TicketKind.MAINTENANCE,
            status=TicketStatus.OPEN,
            student_note="Drum rattles",
        )
        response = self.client.get("/api/v1/admin/dashboard/attention")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        kinds = {i["kind"] for i in response.data}
        self.assertIn("maintenance_reports", kinds)

    def test_attention_flags_offline_machines(self):
        self.washer.is_offline = True
        self.washer.save(update_fields=["is_offline"])
        response = self.client.get("/api/v1/admin/dashboard/attention")
        self.assertIn("machine_offline", {i["kind"] for i in response.data})

    def test_attention_is_empty_on_a_quiet_day(self):
        self.assertEqual(self.client.get("/api/v1/admin/dashboard/attention").data, [])

    def test_activity_merges_bookings_and_admin_actions(self):
        self.add_booking()
        self.client.post(f"/api/v1/admin/students/{self.student.id}/disable/")
        response = self.client.get("/api/v1/admin/activity")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        kinds = {i["kind"] for i in response.data}
        self.assertEqual(kinds, {"booking", "admin_action"})

    def test_activity_is_newest_first(self):
        self.add_booking()
        self.client.post(f"/api/v1/admin/students/{self.student.id}/disable/")
        stamps = [i["at"] for i in self.client.get("/api/v1/admin/activity").data]
        self.assertEqual(stamps, sorted(stamps, reverse=True))


class AdminPasswordTests(AdminOpsWorldMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_change_own_password(self):
        response = self.client.post(
            "/api/v1/admin/me/change-password",
            {"current_password": self.password, "new_password": "N3w-Str0ng-Pass!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.admin_user.refresh_from_db()
        self.assertTrue(self.admin_user.check_password("N3w-Str0ng-Pass!"))

    def test_wrong_current_password_is_refused(self):
        """A hijacked session must not be able to lock the owner out."""
        response = self.client.post(
            "/api/v1/admin/me/change-password",
            {"current_password": "wrong", "new_password": "N3w-Str0ng-Pass!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.admin_user.refresh_from_db()
        self.assertTrue(self.admin_user.check_password(self.password))

    def test_weak_new_password_is_refused(self):
        response = self.client.post(
            "/api/v1/admin/me/change-password",
            {"current_password": self.password, "new_password": "123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_is_logged(self):
        self.client.post(
            "/api/v1/admin/me/change-password",
            {"current_password": self.password, "new_password": "N3w-Str0ng-Pass!"},
            format="json",
        )
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action=AdminAuditLog.Action.ADMIN_PASSWORD_CHANGED
            ).exists()
        )


class AdminOpsAccessTests(AdminOpsWorldMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.as_student = APIClient()
        self.as_student.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {RefreshToken.for_user(self.student.user).access_token}"
            )
        )

    def test_students_cannot_reach_any_admin_ops_route(self):
        for path in (
            "/api/v1/admin/audit-log",
            "/api/v1/admin/dashboard/summary",
            "/api/v1/admin/dashboard/attention",
            "/api/v1/admin/activity",
            f"/api/v1/admin/machines/{self.washer.id}/offline-impact",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.as_student.get(path).status_code, status.HTTP_403_FORBIDDEN
                )

    def test_anonymous_cannot_reach_them(self):
        anon = APIClient()
        self.assertEqual(
            anon.get("/api/v1/admin/audit-log").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
