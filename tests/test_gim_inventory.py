from django.test import TestCase

from laundry.data.gim_inventory import (
    GIM_HOSTEL_NAMES,
    GIM_MACHINE_ROWS,
    build_location_name,
    expected_machines_by_hostel,
    normalize_floor,
)
from laundry.models import MachineKind
from laundry.services.floors import floor_from_location


class GimInventoryTests(TestCase):
    def test_hostel_count(self):
        self.assertEqual(len(GIM_HOSTEL_NAMES), 11)

    def test_total_machine_rows(self):
        self.assertEqual(len(GIM_MACHINE_ROWS), 48)

    def test_normalize_floor(self):
        self.assertEqual(normalize_floor("basement"), "Basement")
        self.assertEqual(normalize_floor("ground"), "Ground Floor")
        self.assertEqual(normalize_floor("third"), "3rd Floor")

    def test_build_location_name_first_and_duplicate(self):
        self.assertEqual(build_location_name("ground", MachineKind.WASHER, 1), "Ground Floor · Washer")
        self.assertEqual(
            build_location_name("ground", MachineKind.WASHER, 2),
            "Ground Floor · Washer 2",
        )
        self.assertEqual(build_location_name("basement", MachineKind.DRYER, 1), "Basement · Dryer")

    def test_expected_machines_totals(self):
        by_hostel = expected_machines_by_hostel()
        self.assertEqual(set(by_hostel.keys()), set(GIM_HOSTEL_NAMES))
        total = sum(len(machines) for machines in by_hostel.values())
        self.assertEqual(total, 48)
        self.assertEqual(len(by_hostel["Hostel 9D"]), 7)
        self.assertEqual(len(by_hostel["Hostel 10"]), 8)

    def test_floor_from_location_recognizes_basement(self):
        self.assertEqual(floor_from_location("Basement · Washer"), "Basement")
