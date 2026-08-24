"""Idempotent pilot campus: GIM hostels, machines, and sample users."""

from datetime import time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from laundry.data.gim_inventory import (
    GIM_HOSTEL_NAMES,
    expected_machines_by_hostel,
)
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
from laundry.services.notifications import get_or_create_preferences

User = get_user_model()

INSTITUTE_NAME = "Goa Institute of Management"
ALLOWED_DOMAINS = ["gim.ac.in", "student.gim.ac.in"]

SUPER_EMAIL = "super@lundrii.local"
SUPER_PASSWORD = "LundriiSuper!1"
SUPER_NAME = "Lundrii Platform"

ADMIN_EMAIL = "admin@lundrii.local"
ADMIN_PASSWORD = "LundriiAdmin!1"
ADMIN_NAME = "GIM Laundry Committee"

STUDENT_PASSWORD = "LundriiStudent!1"
DEFAULT_PILOT_HOSTEL = "Hostel 1"

PILOT_STUDENTS = (
    ("aarav.mehta@gim.ac.in", "Aarav Mehta", "+91 98220 41127", Gender.MALE),
    ("rohan.shetty@gim.ac.in", "Rohan Shetty", "", Gender.MALE),
    ("diya.nair@gim.ac.in", "Diya Nair", "", Gender.FEMALE),
)


def _upsert_user(email: str, password: str, *, is_staff: bool, is_superuser: bool):
    email = email.strip().lower()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        return User.objects.create_user(
            email=email,
            password=password,
            is_staff=is_staff,
            is_superuser=is_superuser,
        )
    user.set_password(password)
    user.is_staff = is_staff
    user.is_superuser = is_superuser
    user.is_active = True
    user.save(update_fields=["password", "is_staff", "is_superuser", "is_active", "updated_at"])
    return user


def _upsert_hostel(institute: Institute, name: str) -> Hostel:
    hostel, _ = Hostel.objects.get_or_create(
        institute=institute,
        name=name,
        defaults={"is_active": True},
    )
    if not hostel.is_active:
        hostel.is_active = True
        hostel.save(update_fields=["is_active", "updated_at"])
    return hostel


def _upsert_machine(hostel: Hostel, kind: str, location_name: str, *, is_offline: bool) -> Machine:
    machine = (
        Machine.objects.filter(hostel=hostel, kind=kind, location_name=location_name)
        .order_by("created_at")
        .first()
    )
    defaults = {
        "operating_window_start": time(0, 0),
        "operating_window_end": time(0, 0),
        "slot_length_minutes": 60,
        "is_offline": is_offline,
        "is_active": True,
    }
    if machine is None:
        return Machine.objects.create(hostel=hostel, kind=kind, location_name=location_name, **defaults)
    machine.operating_window_start = defaults["operating_window_start"]
    machine.operating_window_end = defaults["operating_window_end"]
    machine.slot_length_minutes = 60
    machine.is_offline = is_offline
    machine.is_active = True
    machine.save(
        update_fields=[
            "operating_window_start",
            "operating_window_end",
            "slot_length_minutes",
            "is_offline",
            "is_active",
            "updated_at",
        ]
    )
    return machine


def _deactivate_legacy_inventory(institute: Institute, fallback_hostel: Hostel) -> None:
    canonical = set(GIM_HOSTEL_NAMES)
    legacy_hostels = Hostel.objects.filter(institute=institute).exclude(name__in=canonical)
    legacy_ids = list(legacy_hostels.values_list("id", flat=True))
    if legacy_ids:
        Machine.objects.filter(hostel_id__in=legacy_ids, is_active=True).update(is_active=False)
        legacy_hostels.update(is_active=False)
        Student.objects.filter(
            institute=institute,
            home_hostel_id__in=legacy_ids,
        ).update(home_hostel=fallback_hostel)


def _sync_hostel_machines(hostel: Hostel, expected: list[tuple[str, str]]) -> None:
    keep: set[tuple[str, str]] = set()
    for kind, location_name in expected:
        _upsert_machine(hostel, kind, location_name, is_offline=False)
        keep.add((kind, location_name))

    stale = Machine.objects.filter(hostel=hostel, is_active=True)
    for machine in stale:
        key = (machine.kind, machine.location_name)
        if key not in keep:
            machine.is_active = False
            machine.save(update_fields=["is_active", "updated_at"])


def _upsert_student(
    institute: Institute,
    home_hostel: Hostel,
    *,
    email: str,
    password: str,
    name: str,
    phone: str,
    gender: str,
) -> Student:
    student_user = _upsert_user(email, password, is_staff=True, is_superuser=False)
    student, created = Student.objects.get_or_create(
        user=student_user,
        defaults={
            "institute": institute,
            "name": name,
            "phone": phone,
            "whatsapp_opt_in": bool(phone),
            "gender": gender,
            "home_hostel": home_hostel,
            "floor": "",
            "email_verified_at": timezone.now(),
            "is_active": True,
        },
    )
    if not created:
        student.institute = institute
        student.name = name
        student.phone = phone
        student.whatsapp_opt_in = bool(phone)
        student.gender = gender
        student.home_hostel = home_hostel
        student.floor = ""
        student.is_active = True
        if student.email_verified_at is None:
            student.email_verified_at = timezone.now()
        student.save()
    get_or_create_preferences(student)
    return student


class Command(BaseCommand):
    help = "Seed the GIM pilot institute, hostels, machines, and sample users (idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        institute, _ = Institute.objects.get_or_create(
            name=INSTITUTE_NAME,
            defaults={"allowed_email_domains": ALLOWED_DOMAINS, "is_active": True},
        )
        institute.allowed_email_domains = list(ALLOWED_DOMAINS)
        institute.is_active = True
        institute.save(update_fields=["allowed_email_domains", "is_active", "updated_at"])

        InstituteRule.objects.update_or_create(
            institute=institute,
            defaults={
                "quota_limit": 3,
                "quota_window_days": 7,
                "cooldown_hours": 0,
                "advance_window_days": 7,
                "cancellation_cutoff_hours": 6,
                "dryer_cap_enabled": False,
                "is_active": True,
            },
        )

        inventory = expected_machines_by_hostel()
        hostels = {name: _upsert_hostel(institute, name) for name in GIM_HOSTEL_NAMES}
        pilot_hostel = hostels[DEFAULT_PILOT_HOSTEL]

        _deactivate_legacy_inventory(institute, pilot_hostel)

        for name, machines in inventory.items():
            _sync_hostel_machines(hostels[name], machines)

        super_user = _upsert_user(
            SUPER_EMAIL, SUPER_PASSWORD, is_staff=True, is_superuser=True
        )
        SuperAdministrator.objects.update_or_create(
            user=super_user,
            defaults={"display_name": SUPER_NAME, "is_active": True},
        )

        admin_user = _upsert_user(
            ADMIN_EMAIL, ADMIN_PASSWORD, is_staff=True, is_superuser=False
        )
        Administrator.objects.update_or_create(
            user=admin_user,
            defaults={
                "institute": institute,
                "display_name": ADMIN_NAME,
                "is_active": True,
            },
        )

        for email, name, phone, gender in PILOT_STUDENTS:
            _upsert_student(
                institute,
                pilot_hostel,
                email=email,
                password=STUDENT_PASSWORD,
                name=name,
                phone=phone,
                gender=gender,
            )

        active_machines = Machine.objects.filter(
            hostel__institute=institute,
            hostel__is_active=True,
            is_active=True,
        )
        washer_count = active_machines.filter(kind=MachineKind.WASHER).count()
        dryer_count = active_machines.filter(kind=MachineKind.DRYER).count()
        hostel_count = Hostel.objects.filter(institute=institute, is_active=True).count()

        self.stdout.write(self.style.SUCCESS("Pilot seed complete (safe to re-run)."))
        self.stdout.write("")
        self.stdout.write(f"Institute:  {INSTITUTE_NAME}")
        self.stdout.write(f"Domains:    {', '.join(ALLOWED_DOMAINS)}")
        self.stdout.write(
            "Rules:      quota 3/7d · advance 7d · cancel cutoff 6h · dryer_cap off"
        )
        self.stdout.write(f"Hostels:    {hostel_count} active ({', '.join(GIM_HOSTEL_NAMES)})")
        self.stdout.write(
            f"Machines:   {washer_count} washers, {dryer_count} dryers ({washer_count + dryer_count} total)"
        )
        self.stdout.write("")
        self.stdout.write(self.style.NOTICE("Django admin  →  http://127.0.0.1:8000/admin/"))
        self.stdout.write(f"  SuperAdministrator  {SUPER_EMAIL}   /  {SUPER_PASSWORD}")
        self.stdout.write(f"  Administrator       {ADMIN_EMAIL}   /  {ADMIN_PASSWORD}")
        self.stdout.write("")
        self.stdout.write(self.style.NOTICE("Student API login (email + password → JWT):"))
        self.stdout.write(
            f"  POST /api/v1/auth/login   {{\"email\": \"<student>\", \"password\": \"{STUDENT_PASSWORD}\"}}"
        )
        self.stdout.write("")
        self.stdout.write(
            self.style.NOTICE(f"Pilot students ({DEFAULT_PILOT_HOSTEL}, verified):")
        )
        for email, name, _phone, _gender in PILOT_STUDENTS:
            self.stdout.write(f"  {name:<16}  {email}  /  {STUDENT_PASSWORD}")
        self.stdout.write("  Docs:  /api/docs/    Schema:  /api/schema/")
