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
        self.assertFalse(rules.dryer_cap_enabled)

        self.assertEqual(Hostel.objects.filter(institute=institute).count(), 3)
        boys1 = Hostel.objects.get(institute=institute, name="Boys Hostel 1")
        self.assertEqual(boys1.gender, Gender.MALE)

        self.assertEqual(
            Machine.objects.filter(hostel=boys1, kind=MachineKind.WASHER).count(), 5
        )
        offline = Machine.objects.get(
            hostel=boys1, kind=MachineKind.WASHER, location_name="4th Floor · B Wing"
        )
        self.assertTrue(offline.is_offline)

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
        self.assertEqual(student.home_hostel_id, boys1.id)
        self.assertEqual(student.gender, Gender.MALE)
        self.assertTrue(student.user.check_password("LundriiStudent!1"))

        self.assertIn("POST /api/v1/auth/login", out.getvalue())
