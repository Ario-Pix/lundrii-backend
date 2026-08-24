from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from laundry.models import (
    Administrator,
    Gender,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    MachineKind,
    Student,
    SuperAdministrator,
)

User = get_user_model()


class SeedPilotCommandTests(TestCase):
    def test_seed_is_idempotent(self):
        out = StringIO()
        call_command("seed_pilot", stdout=out)
        call_command("seed_pilot", stdout=out)

        self.assertEqual(
            Institute.objects.filter(name="Goa Institute of Management").count(), 1
        )
        institute = Institute.objects.get(name="Goa Institute of Management")
        self.assertEqual(institute.allowed_email_domains, ["gim.ac.in", "student.gim.ac.in"])

        rules = InstituteRule.objects.get(institute=institute)
        self.assertEqual(rules.quota_limit, 3)
        self.assertEqual(rules.quota_window_days, 7)
        self.assertEqual(rules.cooldown_hours, 0)
        self.assertEqual(rules.advance_window_days, 7)
        self.assertEqual(rules.cancellation_cutoff_hours, 6)
        self.assertTrue(rules.dryer_cap_enabled)

        self.assertEqual(
            Hostel.objects.filter(institute=institute, is_active=True).count(), 11
        )
        hostel1 = Hostel.objects.get(institute=institute, name="Hostel 1", is_active=True)
        hostel9d = Hostel.objects.get(institute=institute, name="Hostel 9D", is_active=True)
        hostel10 = Hostel.objects.get(institute=institute, name="Hostel 10", is_active=True)

        active = Machine.objects.filter(
            hostel__institute=institute,
            hostel__is_active=True,
            is_active=True,
        )
        self.assertEqual(active.filter(kind=MachineKind.WASHER).count(), 24)
        self.assertEqual(active.filter(kind=MachineKind.DRYER).count(), 24)
        self.assertEqual(active.count(), 48)

        self.assertEqual(
            active.filter(hostel=hostel9d).count(),
            7,
        )
        self.assertEqual(
            active.filter(hostel=hostel10).count(),
            8,
        )
        self.assertTrue(
            active.filter(
                hostel=hostel1,
                kind=MachineKind.WASHER,
                location_name="Ground Floor · Washer",
            ).exists()
        )
        self.assertTrue(
            active.filter(
                hostel=hostel1,
                kind=MachineKind.WASHER,
                location_name="Ground Floor · Washer 2",
            ).exists()
        )

        self.assertFalse(
            Hostel.objects.filter(
                institute=institute,
                name="Boys Hostel 1",
                is_active=True,
            ).exists()
        )

        self.assertTrue(
            SuperAdministrator.objects.filter(user__email="super@lundrii.local").exists()
        )
        self.assertTrue(
            Administrator.objects.filter(
                user__email="admin@lundrii.local", institute=institute
            ).exists()
        )
        student = Student.objects.get(user__email="aarav.mehta@gim.ac.in")
        self.assertIsNotNone(student.email_verified_at)
        self.assertEqual(student.home_hostel_id, hostel1.id)
        self.assertEqual(student.gender, Gender.MALE)
        self.assertTrue(student.user.check_password("LundriiStudent!1"))

        self.assertIn("POST /api/v1/auth/login", out.getvalue())
