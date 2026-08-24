"""
A machine can never be held by two students at once.

Two ways that guarantee used to break, both covered here:

* **Overlap.** The slot grid is derived, not stored. Narrowing a machine's slot
  length or shifting its operating window re-cuts the grid under bookings that
  already exist, so a freshly derived slot can straddle an old booking while
  starting at a different minute. Comparing only `starts_at` for equality waves
  those through.
* **The check-then-act race.** Testing "is this slot free?" outside the
  transaction that writes lets two concurrent requests both read free and both
  write.
"""

import threading
from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from base.exceptions import APIError
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
    create_bookings,
    move_booking,
    overlapping_bookings,
)

User = get_user_model()


class BookingWorldMixin:
    def make_world(self, *, slot_minutes=60):
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
            slot_length_minutes=slot_minutes,
        )
        self.day = timezone.localdate() + timedelta(days=2)

    def make_student(self, email, name="S"):
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

    def book(self, student, *, hour=None, starts_at=None):
        req = BookingRequest(machine_id=self.washer.id)
        if starts_at is not None:
            req.starts_at = starts_at
        else:
            req.date, req.hour = self.day, hour
        return create_bookings(student, [req])[0]

    def assert_no_overlaps(self):
        live = list(
            Booking.objects.filter(
                machine=self.washer, is_active=True, cancelled_at__isnull=True
            ).order_by("starts_at")
        )
        clashes = [
            (a, b)
            for a, b in zip(live, live[1:])
            if a.ends_at > b.starts_at
        ]
        self.assertEqual(
            clashes,
            [],
            "machine is double-booked: "
            + " | ".join(
                f"{timezone.localtime(a.starts_at):%H:%M}-{timezone.localtime(a.ends_at):%H:%M}"
                f" vs {timezone.localtime(b.starts_at):%H:%M}-{timezone.localtime(b.ends_at):%H:%M}"
                for a, b in clashes
            ),
        )


class OverlapQueryTests(BookingWorldMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.student = self.make_student("a@gim.ac.in")
        self.existing = Booking.objects.create(
            student=self.student,
            machine=self.washer,
            starts_at=self.at(14),
            ends_at=self.at(15),
        )

    def test_identical_interval_overlaps(self):
        self.assertTrue(
            overlapping_bookings(self.washer, self.at(14), self.at(15)).exists()
        )

    def test_partial_overlaps_are_caught_from_both_sides(self):
        for label, start, end in (
            ("starts inside", self.at(14, 30), self.at(15, 30)),
            ("ends inside", self.at(13, 30), self.at(14, 30)),
            ("contains", self.at(13), self.at(16)),
            ("contained by", self.at(14, 15), self.at(14, 45)),
        ):
            with self.subTest(case=label):
                self.assertTrue(
                    overlapping_bookings(self.washer, start, end).exists()
                )

    def test_touching_intervals_do_not_overlap(self):
        """Back-to-back washer→dryer runs depend on this being allowed."""
        self.assertFalse(
            overlapping_bookings(self.washer, self.at(15), self.at(16)).exists()
        )
        self.assertFalse(
            overlapping_bookings(self.washer, self.at(13), self.at(14)).exists()
        )

    def test_cancelled_bookings_do_not_block(self):
        self.existing.cancelled_at = timezone.now()
        self.existing.save(update_fields=["cancelled_at"])
        self.assertFalse(
            overlapping_bookings(self.washer, self.at(14), self.at(15)).exists()
        )

    def test_excluded_booking_does_not_block_itself(self):
        self.assertFalse(
            overlapping_bookings(
                self.washer,
                self.at(14),
                self.at(15),
                exclude_booking_id=self.existing.pk,
            ).exists()
        )

    def test_other_machines_do_not_block(self):
        other = Machine.objects.create(
            hostel=self.hostel,
            kind=MachineKind.WASHER,
            location_name="Other",
            operating_window_start=time(0, 0),
            operating_window_end=time(0, 0),
            slot_length_minutes=60,
        )
        self.assertFalse(
            overlapping_bookings(other, self.at(14), self.at(15)).exists()
        )


class RegridOverlapTests(BookingWorldMixin, TestCase):
    """
    The real-world route to a double booking: an admin re-cuts the slot grid
    under bookings that already exist.
    """

    def setUp(self):
        self.make_world(slot_minutes=60)
        self.first = self.make_student("a@gim.ac.in", "Aarav")
        self.second = self.make_student("b@gim.ac.in", "Riya")

    def test_shortening_slot_length_cannot_double_book(self):
        self.assertTrue(self.book(self.first, hour=14).ok)

        # Admin halves the slot length: 14:30 is now a derivable slot that
        # straddles the 14:00–15:00 booking.
        self.washer.slot_length_minutes = 30
        self.washer.save(update_fields=["slot_length_minutes"])

        result = self.book(self.second, starts_at=self.at(14, 30))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "SLOT_TAKEN")
        self.assert_no_overlaps()

    def test_lengthening_slot_length_cannot_double_book(self):
        self.washer.slot_length_minutes = 30
        self.washer.save(update_fields=["slot_length_minutes"])
        self.assertTrue(self.book(self.first, starts_at=self.at(14, 30)).ok)

        # Now 14:00–15:00 would swallow the 14:30–15:00 booking.
        self.washer.slot_length_minutes = 60
        self.washer.save(update_fields=["slot_length_minutes"])

        result = self.book(self.second, hour=14)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "SLOT_TAKEN")
        self.assert_no_overlaps()

    def test_shifting_the_operating_window_cannot_double_book(self):
        self.assertTrue(self.book(self.first, hour=14).ok)

        # Window now starts on the half hour, so the grid lands on :30.
        self.washer.operating_window_start = time(0, 30)
        self.washer.operating_window_end = time(0, 30)
        self.washer.save(
            update_fields=["operating_window_start", "operating_window_end"]
        )

        result = self.book(self.second, starts_at=self.at(14, 30))
        self.assertFalse(result.ok)
        self.assert_no_overlaps()

    def test_adjacent_slots_still_bookable(self):
        """The fix must not over-reject: 15:00 is genuinely free."""
        self.assertTrue(self.book(self.first, hour=14).ok)
        self.assertTrue(self.book(self.second, hour=15).ok)
        self.assertEqual(Booking.objects.count(), 2)
        self.assert_no_overlaps()


class MoveOverlapTests(BookingWorldMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.first = self.make_student("a@gim.ac.in", "Aarav")
        self.second = self.make_student("b@gim.ac.in", "Riya")

    def test_cannot_move_onto_an_overlapping_slot(self):
        self.assertTrue(self.book(self.first, hour=14).ok)
        mine = self.book(self.second, hour=18).booking

        self.washer.slot_length_minutes = 30
        self.washer.save(update_fields=["slot_length_minutes"])

        with self.assertRaises(APIError) as caught:
            move_booking(
                self.second,
                mine,
                BookingRequest(machine_id=self.washer.id, starts_at=self.at(14, 30)),
            )
        self.assertEqual(caught.exception.code, "SLOT_TAKEN")
        self.assert_no_overlaps()

    def test_moving_to_a_genuinely_free_slot_still_works(self):
        mine = self.book(self.first, hour=14).booking
        moved = move_booking(
            self.first,
            mine,
            BookingRequest(machine_id=self.washer.id, date=self.day, hour=20),
        )
        self.assertEqual(timezone.localtime(moved.starts_at).hour, 20)
        self.assert_no_overlaps()

    def test_moving_a_booking_onto_its_own_slot_is_not_a_self_collision(self):
        mine = self.book(self.first, hour=14).booking
        moved = move_booking(
            self.first,
            mine,
            BookingRequest(machine_id=self.washer.id, date=self.day, hour=14),
        )
        self.assertEqual(timezone.localtime(moved.starts_at).hour, 14)


class ConcurrentClaimTests(BookingWorldMixin, TransactionTestCase):
    """
    Real threads racing for one slot. TransactionTestCase (not TestCase)
    because each thread needs its own committed connection — inside a single
    wrapping test transaction the race cannot happen at all.
    """

    def setUp(self):
        self.make_world()
        self.students = [
            self.make_student(f"s{i}@gim.ac.in", f"S{i}") for i in range(8)
        ]

    def tearDown(self):
        for conn in connections.all():
            conn.close()

    def _race(self, students, **slot):
        results, errors = [], []
        barrier = threading.Barrier(len(students))

        def claim(student):
            try:
                barrier.wait(timeout=10)
                results.append(self.book(student, **slot))
            except Exception as exc:  # noqa: BLE001 — surfaced below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=claim, args=(s,)) for s in students]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return results, errors

    def test_only_one_of_eight_concurrent_claims_wins(self):
        results, errors = self._race(self.students, hour=14)
        self.assertEqual(errors, [], f"threads raised: {errors}")

        winners = [r for r in results if r.ok]
        self.assertEqual(
            len(winners), 1, f"expected exactly one winner, got {len(winners)}"
        )
        for loser in (r for r in results if not r.ok):
            self.assertEqual(loser.code, "SLOT_TAKEN")

        self.assertEqual(
            Booking.objects.filter(
                machine=self.washer, cancelled_at__isnull=True
            ).count(),
            1,
        )
        self.assert_no_overlaps()

    def test_concurrent_claims_on_different_slots_all_succeed(self):
        """The lock must serialize contention, not block unrelated bookings."""
        results, errors = [], []
        barrier = threading.Barrier(len(self.students))

        def claim(student, hour):
            try:
                barrier.wait(timeout=10)
                results.append(self.book(student, hour=hour))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=claim, args=(s, 8 + i))
            for i, s in enumerate(self.students)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"threads raised: {errors}")
        self.assertEqual(len([r for r in results if r.ok]), len(self.students))
        self.assert_no_overlaps()
