"""Focused tests for slot derivation, fairness rules, and booking services."""

from datetime import datetime, time, timedelta

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
    Student,
)
from laundry.services.booking import (
    BookingRequest,
    cancel_booking,
    create_bookings,
    move_booking,
)
from laundry.services.rules import (
    RULE_QUOTA,
    check_booking_rules,
    get_institute_rules,
)
from laundry.services.slots import (
    SLOT_FREE,
    SLOT_MINE,
    SLOT_OFFLINE,
    SLOT_PAST,
    SLOT_RUNNING,
    SLOT_TAKEN,
    derive_slots,
    iter_operating_slots,
    resolve_slot,
)

User = get_user_model()


def aware(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute))


class FixtureMixin:
    def make_world(self, **rule_kwargs):
        self.institute = Institute.objects.create(
            name="GIM Test",
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
        self.dryer = Machine.objects.create(
            hostel=self.boys,
            kind=MachineKind.DRYER,
            location_name="Ground Floor · B Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.now = aware(2026, 8, 9, 10, 0)
        self.student = self.make_student("aarav@gim.ac.in", "Aarav Mehta", Gender.MALE)

    def make_student(self, email, name, gender, *, verified=True, suspended_until=None):
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

    def add_booking(self, student, machine, starts_at, *, cancelled_at=None, late=False, counts=True):
        return Booking.objects.create(
            student=student,
            machine=machine,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            cancelled_at=cancelled_at,
            is_late_cancel=late,
            counts_against_quota=counts,
        )


class SlotDerivationTests(FixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_24h_hourly_yields_24_slots(self):
        slots = list(iter_operating_slots(self.washer, self.now.date()))
        self.assertEqual(len(slots), 24)
        self.assertEqual(slots[0][0], aware(2026, 8, 9, 0, 0))
        self.assertEqual(slots[-1][1], aware(2026, 8, 10, 0, 0))

    def test_custom_window(self):
        self.washer.operating_window_start = time(8, 0)
        self.washer.operating_window_end = time(12, 0)
        self.washer.slot_length_minutes = 60
        slots = list(iter_operating_slots(self.washer, self.now.date()))
        self.assertEqual(len(slots), 4)
        self.assertEqual(slots[0][0].hour, 8)
        self.assertEqual(slots[-1][0].hour, 11)

    def test_overlay_states(self):
        peer = self.make_student("peer@gim.ac.in", "Priya", Gender.MALE)
        self.add_booking(self.student, self.washer, aware(2026, 8, 9, 13))
        self.add_booking(peer, self.washer, aware(2026, 8, 9, 14))
        self.add_booking(peer, self.washer, aware(2026, 8, 9, 10))  # running at now=10:00

        slots = {s.hour: s for s in derive_slots(self.washer, self.now.date(), student=self.student, now=self.now)}
        self.assertEqual(slots[9].state, SLOT_PAST)
        self.assertEqual(slots[10].state, SLOT_RUNNING)
        self.assertEqual(slots[13].state, SLOT_MINE)
        self.assertEqual(slots[14].state, SLOT_TAKEN)
        self.assertEqual(slots[14].holder_name, "Priya")
        self.assertEqual(slots[20].state, SLOT_FREE)

    def test_offline_future_slots(self):
        self.washer.is_offline = True
        self.washer.save(update_fields=["is_offline"])
        slots = {s.hour: s for s in derive_slots(self.washer, self.now.date(), student=self.student, now=self.now)}
        self.assertEqual(slots[9].state, SLOT_PAST)
        self.assertEqual(slots[15].state, SLOT_OFFLINE)

    def test_previous_wash_does_not_block_later_slots(self):
        self.add_booking(self.student, self.washer, aware(2026, 8, 9, 8))
        slots = {s.hour: s for s in derive_slots(self.washer, self.now.date(), student=self.student, now=self.now)}
        self.assertEqual(slots[14].state, SLOT_FREE)
        self.assertIsNone(slots[14].blocked_rule)
        self.assertEqual(slots[15].state, SLOT_FREE)

    def test_resolve_slot(self):
        start, end = resolve_slot(self.washer, aware(2026, 8, 9, 15))
        self.assertEqual(start, aware(2026, 8, 9, 15))
        self.assertEqual(end, aware(2026, 8, 9, 16))
        self.assertIsNone(resolve_slot(self.washer, aware(2026, 8, 9, 15, 30)))

    def test_slot_hours_are_ist_wall_clock(self):
        from datetime import UTC

        local_1400 = aware(2026, 8, 9, 14)
        matched = resolve_slot(self.washer, local_1400)
        self.assertIsNotNone(matched)
        self.assertEqual(timezone.localtime(matched[0]).hour, 14)
        # 14:00 IST is 08:30 UTC — same instant, same slot.
        as_utc = datetime(2026, 8, 9, 8, 30, tzinfo=UTC)
        matched_utc = resolve_slot(self.washer, as_utc)
        self.assertEqual(matched[0], matched_utc[0])
        # 14:00Z is 19:30 IST, which is not a slot start.
        self.assertIsNone(resolve_slot(self.washer, datetime(2026, 8, 9, 14, 0, tzinfo=UTC)))


class RulesEngineTests(FixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_past_slot(self):
        block = check_booking_rules(
            self.student,
            self.washer,
            aware(2026, 8, 9, 9),
            aware(2026, 8, 9, 10),
            now=self.now,
        )
        self.assertIsNotNone(block)
        self.assertEqual(block.code, "PAST_SLOT")

    def test_advance_window(self):
        start = aware(2026, 8, 20, 10)
        block = check_booking_rules(
            self.student,
            self.washer,
            start,
            start + timedelta(hours=1),
            now=self.now,
        )
        self.assertIsNotNone(block)
        self.assertEqual(block.code, "OUTSIDE_ADVANCE_WINDOW")
        self.assertIsNotNone(block.clears_at)

    def test_quota_block_and_clears_at(self):
        self.add_booking(self.student, self.washer, aware(2026, 8, 4, 10))
        self.add_booking(self.student, self.washer, aware(2026, 8, 6, 10))
        self.add_booking(self.student, self.washer, aware(2026, 8, 8, 10))
        start = aware(2026, 8, 9, 16)
        block = check_booking_rules(
            self.student,
            self.washer,
            start,
            start + timedelta(hours=1),
            now=self.now,
        )
        self.assertIsNotNone(block)
        self.assertEqual(block.rule, RULE_QUOTA)
        self.assertEqual(block.code, "RULE_BLOCKED")
        self.assertEqual(block.clears_at, aware(2026, 8, 10, 0))

    def test_quota_is_monday_to_sunday(self):
        # Three this week (Mon 3 Aug – Sun 9 Aug) must not block next Monday.
        self.add_booking(self.student, self.washer, aware(2026, 8, 4, 10))
        self.add_booking(self.student, self.washer, aware(2026, 8, 6, 10))
        self.add_booking(self.student, self.washer, aware(2026, 8, 8, 10))
        next_monday = aware(2026, 8, 10, 10)
        self.assertIsNone(
            check_booking_rules(
                self.student,
                self.washer,
                next_monday,
                next_monday + timedelta(hours=1),
                now=self.now,
            )
        )

    def test_cooldown_is_not_enforced(self):
        self.rules.cooldown_hours = 6
        self.rules.save(update_fields=["cooldown_hours"])
        self.add_booking(self.student, self.washer, aware(2026, 8, 9, 8))  # ends 09:00
        start = aware(2026, 8, 9, 14)
        self.assertIsNone(
            check_booking_rules(
                self.student,
                self.washer,
                start,
                start + timedelta(hours=1),
                now=self.now,
            )
        )

    def test_dryer_skips_quota_unless_cap(self):
        self.add_booking(self.student, self.washer, aware(2026, 8, 4, 10))
        self.add_booking(self.student, self.washer, aware(2026, 8, 6, 10))
        self.add_booking(self.student, self.washer, aware(2026, 8, 8, 10))
        start = aware(2026, 8, 9, 16)
        self.assertIsNone(
            check_booking_rules(
                self.student,
                self.dryer,
                start,
                start + timedelta(hours=1),
                now=self.now,
            )
        )
        self.rules.dryer_cap_enabled = True
        self.rules.save(update_fields=["dryer_cap_enabled"])
        # Drop cached O2O
        self.institute.refresh_from_db()
        block = check_booking_rules(
            self.student,
            self.dryer,
            start,
            start + timedelta(hours=1),
            now=self.now,
            rules=get_institute_rules(self.institute),
        )
        self.assertIsNotNone(block)
        self.assertEqual(block.rule, RULE_QUOTA)


class BookingServiceTests(FixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_create_and_integrity_slot_taken(self):
        start = aware(2026, 8, 9, 16)
        results = create_bookings(
            self.student,
            [BookingRequest(machine_id=self.washer.id, starts_at=start)],
            now=self.now,
        )
        self.assertTrue(results[0].ok)

        peer = self.make_student("peer@gim.ac.in", "Peer", Gender.MALE)
        taken = create_bookings(
            peer,
            [BookingRequest(machine_id=self.washer.id, starts_at=start)],
            now=self.now,
        )
        self.assertFalse(taken[0].ok)
        self.assertEqual(taken[0].code, "SLOT_TAKEN")

    def test_combined_partial_success(self):
        start = aware(2026, 8, 9, 16)
        peer = self.make_student("peer@gim.ac.in", "Peer", Gender.MALE)
        self.add_booking(peer, self.dryer, start)

        results = create_bookings(
            self.student,
            [
                BookingRequest(machine_id=self.washer.id, starts_at=start),
                BookingRequest(machine_id=self.dryer.id, starts_at=start),
            ],
            now=self.now,
        )
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertEqual(results[1].code, "SLOT_TAKEN")

    def test_free_vs_late_cancel(self):
        far = self.add_booking(self.student, self.washer, aware(2026, 8, 9, 20))
        cancel_booking(self.student, far, now=self.now)
        far.refresh_from_db()
        self.assertFalse(far.is_late_cancel)
        self.assertFalse(far.counts_against_quota)

        near = self.add_booking(self.student, self.washer, aware(2026, 8, 9, 14))
        cancel_booking(self.student, near, now=self.now)
        near.refresh_from_db()
        self.assertTrue(near.is_late_cancel)
        self.assertTrue(near.counts_against_quota)

    def test_move_releases_old_slot(self):
        original = aware(2026, 8, 9, 16)
        dest = aware(2026, 8, 9, 18)
        booking = self.add_booking(self.student, self.washer, original)
        moved = move_booking(
            self.student,
            booking,
            BookingRequest(machine_id=self.washer.id, starts_at=dest),
            now=self.now,
        )
        self.assertEqual(moved.starts_at, dest)
        self.assertFalse(
            Booking.objects.filter(
                machine=self.washer,
                starts_at=original,
                cancelled_at__isnull=True,
            ).exists()
        )

    def test_unverified_blocked(self):
        newbie = self.make_student("new@gim.ac.in", "New", Gender.MALE, verified=False)
        from base.exceptions import APIError

        with self.assertRaises(APIError) as ctx:
            create_bookings(
                newbie,
                [BookingRequest(machine_id=self.washer.id, starts_at=aware(2026, 8, 9, 16))],
                now=self.now,
            )
        self.assertEqual(ctx.exception.code, "UNVERIFIED")


class StudentAPITests(FixtureMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.client = APIClient()
        token = RefreshToken.for_user(self.student.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def test_other_institute_hostel_hidden(self):
        other = Institute.objects.create(
            name="Other Campus",
            allowed_email_domains=["other.edu"],
        )
        other_hostel = Hostel.objects.create(institute=other, name="Other H")
        url = f"/api/v1/hostels/{other_hostel.id}/machines"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_non_home_eligible_hostel_machines_and_booking(self):
        boys_two = Hostel.objects.create(
            institute=self.institute, name="Boys 2"
        )
        washer_two = Machine.objects.create(
            hostel=boys_two,
            kind=MachineKind.WASHER,
            location_name="2nd Floor · A Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.assertEqual(self.student.home_hostel_id, self.boys.id)

        listed = self.client.get(f"/api/v1/hostels/{boys_two.id}/machines")
        self.assertEqual(listed.status_code, 200, listed.data)
        rows = (
            listed.data["results"]
            if isinstance(listed.data, dict) and "results" in listed.data
            else listed.data
        )
        ids = {str(row["id"]) for row in rows}
        self.assertIn(str(washer_two.id), ids)

        start = (timezone.localtime() + timedelta(hours=8)).replace(
            minute=0, second=0, microsecond=0
        )
        booked = self.client.post(
            "/api/v1/bookings",
            {
                "machineId": str(washer_two.id),
                "startsAt": start.isoformat(),
            },
            format="json",
        )
        self.assertEqual(booked.status_code, 200, booked.data)
        self.assertTrue(booked.data["results"][0]["ok"], booked.data)
        self.assertEqual(
            booked.data["results"][0]["booking"]["hostelId"], str(boys_two.id)
        )

    def test_slots_unpaginated(self):
        url = f"/api/v1/machines/{self.washer.id}/slots"
        response = self.client.get(url, {"date": "2026-08-09"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("slots", response.data)
        self.assertNotIn("results", response.data)
        self.assertEqual(len(response.data["slots"]), 24)

    def test_create_booking_api(self):
        start = (timezone.localtime() + timedelta(hours=8)).replace(
            minute=0, second=0, microsecond=0
        )
        response = self.client.post(
            "/api/v1/bookings",
            {
                "machineId": str(self.washer.id),
                "startsAt": start.isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["results"][0]["ok"], response.data)
        self.assertIn("booking", response.data["results"][0])
        self.assertEqual(response.data["results"][0]["booking"]["hour"], start.hour)

    def test_slots_default_date_is_ist_today(self):
        response = self.client.get(f"/api/v1/machines/{self.washer.id}/slots")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["date"], timezone.localdate().isoformat())

    def test_create_booking_ist_offset_not_utc_wall_clock(self):
        from datetime import UTC

        start = (timezone.localtime() + timedelta(days=1)).replace(
            hour=16, minute=0, second=0, microsecond=0
        )
        response = self.client.post(
            "/api/v1/bookings",
            {
                "machineId": str(self.washer.id),
                "startsAt": start.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["results"][0]["ok"], response.data)
        self.assertEqual(response.data["results"][0]["booking"]["hour"], 16)

        # Same digits with Z would be 16:00 UTC (21:30 IST) — not a slot.
        utc_wall = datetime(
            start.year, start.month, start.day, 16, 0, tzinfo=UTC
        )
        rejected = self.client.post(
            "/api/v1/bookings",
            {
                "machineId": str(self.dryer.id),
                "startsAt": utc_wall.isoformat().replace("+00:00", "Z"),
            },
            format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertFalse(rejected.data["results"][0]["ok"], rejected.data)

    def test_naive_starts_at_is_institute_local(self):
        start = (timezone.localtime() + timedelta(hours=9)).replace(
            minute=0, second=0, microsecond=0
        )
        response = self.client.post(
            "/api/v1/bookings",
            {
                "machineId": str(self.washer.id),
                "startsAt": start.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["results"][0]["ok"], response.data)
        self.assertEqual(response.data["results"][0]["booking"]["hour"], start.hour)

    def test_blank_gender_with_home_hostel_can_book(self):
        user = User.objects.create_user(email="nogender@gim.ac.in", password="unused")
        Student.objects.create(
            user=user,
            institute=self.institute,
            name="No Gender Yet",
            gender="",
            home_hostel=self.boys,
            email_verified_at=self.now,
        )
        client = APIClient()
        token = RefreshToken.for_user(user)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

        hostels = client.get("/api/v1/me/hostels")
        self.assertEqual(hostels.status_code, 200, hostels.data)
        names = {row["name"] for row in hostels.data}
        self.assertIn("Boys 1", names)
        self.assertIn("Girls 1", names)

        me = client.get("/api/v1/me")
        self.assertEqual(me.status_code, 200, me.data)
        self.assertIsNone(me.data["gender"])

        start = (timezone.localtime() + timedelta(hours=8)).replace(
            minute=0, second=0, microsecond=0
        )
        booked = client.post(
            "/api/v1/bookings",
            {
                "machineId": str(self.washer.id),
                "startsAt": start.isoformat(),
            },
            format="json",
        )
        self.assertEqual(booked.status_code, 200, booked.data)
        self.assertTrue(booked.data["results"][0]["ok"], booked.data)
        upcoming = client.get("/api/v1/bookings", {"status": "upcoming"})
        self.assertEqual(upcoming.status_code, 200, upcoming.data)
        rows = (
            upcoming.data["results"]
            if isinstance(upcoming.data, dict) and "results" in upcoming.data
            else upcoming.data
        )
        self.assertEqual(len(rows), 1)

    def test_blank_gender_without_hostel_can_still_book(self):
        user = User.objects.create_user(email="nohostel@gim.ac.in", password="unused")
        Student.objects.create(
            user=user,
            institute=self.institute,
            name="Unplaced",
            gender="",
            home_hostel=None,
            email_verified_at=self.now,
        )
        client = APIClient()
        token = RefreshToken.for_user(user)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        start = (timezone.localtime() + timedelta(hours=8)).replace(
            minute=0, second=0, microsecond=0
        )
        booked = client.post(
            "/api/v1/bookings",
            {
                "machineId": str(self.washer.id),
                "startsAt": start.isoformat(),
            },
            format="json",
        )
        self.assertEqual(booked.status_code, 200, booked.data)
        item = booked.data["results"][0]
        self.assertTrue(item["ok"], booked.data)


class GuestScheduleBrowseTests(FixtureMixin, TestCase):
    """Guests may read occupancy; they never get fixture grids or holder PII."""

    def setUp(self):
        self.make_world()
        self.client = APIClient()
        self.girl_washer = Machine.objects.create(
            hostel=self.girls,
            kind=MachineKind.WASHER,
            location_name="1st Floor · A Wing",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )

    def _rows(self, data):
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def test_anonymous_machines_are_database_rows(self):
        response = self.client.get(f"/api/v1/hostels/{self.boys.id}/machines")
        self.assertEqual(response.status_code, 200)
        ids = {str(row["id"]) for row in self._rows(response.data)}
        self.assertIn(str(self.washer.id), ids)
        self.assertIn(str(self.dryer.id), ids)
        self.assertNotIn(str(self.girl_washer.id), ids)

    def test_anonymous_can_browse_any_hostel(self):
        response = self.client.get(f"/api/v1/hostels/{self.girls.id}/machines")
        self.assertEqual(response.status_code, 200)
        ids = {str(row["id"]) for row in self._rows(response.data)}
        self.assertIn(str(self.girl_washer.id), ids)
        self.assertNotIn(str(self.washer.id), ids)
        self.assertNotIn(str(self.dryer.id), ids)

    def test_anonymous_slots_match_occupancy_without_holder(self):
        on = timezone.localdate() + timedelta(days=1)
        start = timezone.make_aware(datetime.combine(on, time(14, 0)))
        self.add_booking(self.student, self.washer, start)
        response = self.client.get(
            f"/api/v1/machines/{self.washer.id}/slots",
            {"date": on.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        slot = next(s for s in response.data["slots"] if s["hour"] == 14)
        self.assertEqual(slot["state"], SLOT_TAKEN)
        self.assertIsNone(slot["holder"])
        self.assertIsNone(slot["bookingId"])
        self.assertEqual(slot["label"], "Taken")

    def test_signed_in_slots_still_name_the_holder(self):
        on = timezone.localdate() + timedelta(days=1)
        start = timezone.make_aware(datetime.combine(on, time(14, 0)))
        peer = self.make_student("peer@gim.ac.in", "Priya Kulkarni", Gender.MALE)
        self.add_booking(peer, self.washer, start)
        authed = APIClient()
        token = RefreshToken.for_user(self.student.user)
        authed.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        response = authed.get(
            f"/api/v1/machines/{self.washer.id}/slots",
            {"date": on.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        slot = next(s for s in response.data["slots"] if s["hour"] == 14)
        self.assertEqual(slot["state"], SLOT_TAKEN)
        self.assertEqual(slot["holder"]["name"], "Priya Kulkarni")

    def test_unplaced_student_can_browse_machines(self):
        user = User.objects.create_user(email="new@gim.ac.in", password="unused")
        Student.objects.create(
            user=user,
            institute=self.institute,
            name="New Kid",
            gender="",
            home_hostel=None,
        )
        authed = APIClient()
        token = RefreshToken.for_user(user)
        authed.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        response = authed.get(f"/api/v1/hostels/{self.boys.id}/machines")
        self.assertEqual(response.status_code, 200)
        ids = {str(row["id"]) for row in self._rows(response.data)}
        self.assertIn(str(self.washer.id), ids)

    def test_unplaced_student_can_browse_slots(self):
        user = User.objects.create_user(email="new2@gim.ac.in", password="unused")
        Student.objects.create(
            user=user,
            institute=self.institute,
            name="New Kid",
            gender="",
            home_hostel=None,
        )
        authed = APIClient()
        token = RefreshToken.for_user(user)
        authed.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        response = authed.get(f"/api/v1/machines/{self.washer.id}/slots")
        self.assertEqual(response.status_code, 200)
        self.assertIn("slots", response.data)

    def test_anonymous_cannot_book(self):
        start = (timezone.localtime() + timedelta(hours=8)).replace(
            minute=0, second=0, microsecond=0
        )
        response = self.client.post(
            "/api/v1/bookings",
            {
                "machineId": str(self.washer.id),
                "startsAt": start.isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 401)
