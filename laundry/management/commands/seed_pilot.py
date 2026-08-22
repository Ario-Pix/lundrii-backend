"""Idempotent pilot campus: GIM hostels, machines, and sample users."""

from datetime import time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

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

PILOT_STUDENTS = (
    ("aarav.mehta@gim.ac.in", "Aarav Mehta", "+91 98220 41127", Gender.MALE),
    ("rohan.shetty@gim.ac.in", "Rohan Shetty", "", Gender.MALE),
    ("diya.nair@gim.ac.in", "Diya Nair", "", Gender.FEMALE),
)

HOSTELS = (
    ("Boys Hostel 1", Gender.MALE),
    ("Boys Hostel 2", Gender.MALE),
    ("PG Block", Gender.MALE),
)

# Flutter mock machines live on Boys Hostel 1.
BH1_MACHINES = (
    (MachineKind.WASHER, "3rd Floor · A Wing", False),
    (MachineKind.WASHER, "2nd Floor · A Wing", False),
    (MachineKind.WASHER, "2nd Floor · C Wing", False),
    (MachineKind.WASHER, "Ground Floor · B Wing", False),
    (MachineKind.WASHER, "4th Floor · B Wing", True),
    (MachineKind.DRYER, "Ground Floor · B Wing", False),
    (MachineKind.DRYER, "2nd Floor · C Wing", False),
)

OTHER_HOSTEL_MACHINES = (
    (MachineKind.WASHER, "Ground Floor", False),
    (MachineKind.DRYER, "Ground Floor", False),
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


def _upsert_hostel(institute: Institute, name: str, gender: str) -> Hostel:
    hostel, _ = Hostel.objects.get_or_create(
        institute=institute,
        name=name,
        defaults={"gender": gender, "is_active": True},
    )
    changed = False
    if hostel.gender != gender:
        hostel.gender = gender
        changed = True
    if not hostel.is_active:
        hostel.is_active = True
        changed = True
    if changed:
        hostel.save(update_fields=["gender", "is_active", "updated_at"])
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


def _upsert_student(
    institute: Institute,
    boys1: Hostel,
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
            "home_hostel": boys1,
            "floor": "3rd Floor",
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
        student.home_hostel = boys1
        student.floor = "3rd Floor"
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

        hostels = {
            name: _upsert_hostel(institute, name, gender) for name, gender in HOSTELS
        }
        boys1 = hostels["Boys Hostel 1"]

        for kind, location, offline in BH1_MACHINES:
            _upsert_machine(boys1, kind, location, is_offline=offline)
        for name in ("Boys Hostel 2", "PG Block"):
            for kind, location, offline in OTHER_HOSTEL_MACHINES:
                _upsert_machine(hostels[name], kind, location, is_offline=offline)

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
                boys1,
                email=email,
                password=STUDENT_PASSWORD,
                name=name,
                phone=phone,
                gender=gender,
            )

        washer_count = Machine.objects.filter(hostel__institute=institute, kind=MachineKind.WASHER).count()
        dryer_count = Machine.objects.filter(hostel__institute=institute, kind=MachineKind.DRYER).count()

        self.stdout.write(self.style.SUCCESS("Pilot seed complete (safe to re-run)."))
        self.stdout.write("")
        self.stdout.write(f"Institute:  {INSTITUTE_NAME}")
        self.stdout.write(f"Domains:    {', '.join(ALLOWED_DOMAINS)}")
        self.stdout.write(
            "Rules:      quota 3/7d · advance 7d · cancel cutoff 6h · dryer_cap off"
        )
        self.stdout.write(
            f"Hostels:    {', '.join(name for name, _ in HOSTELS)} (all male)"
        )
        self.stdout.write(f"Machines:   {washer_count} washers, {dryer_count} dryers (BH1 matches Flutter names; 4th Floor · B Wing offline)")
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
        self.stdout.write(self.style.NOTICE("Pilot students (Boys Hostel 1, verified):"))
        for email, name, _phone, _gender in PILOT_STUDENTS:
            self.stdout.write(f"  {name:<16}  {email}  /  {STUDENT_PASSWORD}")
        self.stdout.write("  Docs:  /api/docs/    Schema:  /api/schema/")
