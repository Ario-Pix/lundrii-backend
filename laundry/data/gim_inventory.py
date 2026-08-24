"""GIM campus laundry inventory — hostels, machines, and floor labels."""

from __future__ import annotations

from collections import defaultdict

from laundry.models import MachineKind

GIM_HOSTEL_NAMES = (
    "Hostel 1",
    "Hostel 2",
    "Hostel 3",
    "Hostel 4",
    "Hostel 5",
    "Hostel 7",
    "Hostel 8",
    "Hostel 9A",
    "Hostel 9C",
    "Hostel 9D",
    "Hostel 10",
)

# Flat rows mirroring the spreadsheet: (hostel_name, kind, floor_key).
# floor_key: basement | ground | first | second | third | fourth
GIM_MACHINE_ROWS: tuple[tuple[str, str, str], ...] = (
    # Hostel 1
    ("Hostel 1", MachineKind.WASHER, "ground"),
    ("Hostel 1", MachineKind.WASHER, "ground"),
    ("Hostel 1", MachineKind.DRYER, "ground"),
    ("Hostel 1", MachineKind.WASHER, "first"),
    ("Hostel 1", MachineKind.DRYER, "first"),
    # Hostel 2
    ("Hostel 2", MachineKind.WASHER, "basement"),
    ("Hostel 2", MachineKind.DRYER, "basement"),
    ("Hostel 2", MachineKind.WASHER, "second"),
    ("Hostel 2", MachineKind.DRYER, "second"),
    # Hostel 3
    ("Hostel 3", MachineKind.WASHER, "ground"),
    ("Hostel 3", MachineKind.DRYER, "ground"),
    ("Hostel 3", MachineKind.DRYER, "ground"),
    # Hostel 4
    ("Hostel 4", MachineKind.WASHER, "basement"),
    ("Hostel 4", MachineKind.WASHER, "basement"),
    ("Hostel 4", MachineKind.DRYER, "basement"),
    # Hostel 5
    ("Hostel 5", MachineKind.WASHER, "basement"),
    ("Hostel 5", MachineKind.DRYER, "basement"),
    ("Hostel 5", MachineKind.DRYER, "basement"),
    # Hostel 7
    ("Hostel 7", MachineKind.WASHER, "first"),
    ("Hostel 7", MachineKind.DRYER, "first"),
    ("Hostel 7", MachineKind.DRYER, "first"),
    # Hostel 8
    ("Hostel 8", MachineKind.WASHER, "first"),
    ("Hostel 8", MachineKind.WASHER, "first"),
    ("Hostel 8", MachineKind.DRYER, "first"),
    ("Hostel 8", MachineKind.DRYER, "first"),
    # Hostel 9A
    ("Hostel 9A", MachineKind.WASHER, "ground"),
    ("Hostel 9A", MachineKind.DRYER, "ground"),
    ("Hostel 9A", MachineKind.WASHER, "third"),
    ("Hostel 9A", MachineKind.DRYER, "third"),
    # Hostel 9C
    ("Hostel 9C", MachineKind.WASHER, "ground"),
    ("Hostel 9C", MachineKind.DRYER, "ground"),
    ("Hostel 9C", MachineKind.WASHER, "third"),
    ("Hostel 9C", MachineKind.DRYER, "third"),
    # Hostel 9D
    ("Hostel 9D", MachineKind.WASHER, "ground"),
    ("Hostel 9D", MachineKind.DRYER, "ground"),
    ("Hostel 9D", MachineKind.WASHER, "second"),
    ("Hostel 9D", MachineKind.WASHER, "second"),
    ("Hostel 9D", MachineKind.DRYER, "second"),
    ("Hostel 9D", MachineKind.WASHER, "fourth"),
    ("Hostel 9D", MachineKind.DRYER, "fourth"),
    # Hostel 10
    ("Hostel 10", MachineKind.WASHER, "first"),
    ("Hostel 10", MachineKind.WASHER, "first"),
    ("Hostel 10", MachineKind.DRYER, "first"),
    ("Hostel 10", MachineKind.DRYER, "first"),
    ("Hostel 10", MachineKind.WASHER, "third"),
    ("Hostel 10", MachineKind.WASHER, "third"),
    ("Hostel 10", MachineKind.DRYER, "third"),
    ("Hostel 10", MachineKind.DRYER, "third"),
)

_FLOOR_LABELS = {
    "basement": "Basement",
    "ground": "Ground Floor",
    "first": "1st Floor",
    "second": "2nd Floor",
    "third": "3rd Floor",
    "fourth": "4th Floor",
}

_KIND_LABELS = {
    MachineKind.WASHER: "Washer",
    MachineKind.DRYER: "Dryer",
}


def normalize_floor(floor_key: str) -> str:
    key = (floor_key or "").strip().lower()
    if key not in _FLOOR_LABELS:
        raise ValueError(f"Unknown floor key: {floor_key!r}")
    return _FLOOR_LABELS[key]


def build_location_name(floor_key: str, kind: str, index_within_group: int) -> str:
    """Build a unique machine location for one hostel floor + kind group."""
    floor = normalize_floor(floor_key)
    label = _KIND_LABELS[kind]
    if index_within_group <= 1:
        return f"{floor} · {label}"
    return f"{floor} · {label} {index_within_group}"


def expected_machines_by_hostel() -> dict[str, list[tuple[str, str]]]:
    """Return {hostel_name: [(kind, location_name), ...]} from inventory rows."""
    counters: dict[tuple[str, str, str], int] = defaultdict(int)
    by_hostel: dict[str, list[tuple[str, str]]] = {name: [] for name in GIM_HOSTEL_NAMES}

    for hostel_name, kind, floor_key in GIM_MACHINE_ROWS:
        counters[(hostel_name, kind, floor_key)] += 1
        idx = counters[(hostel_name, kind, floor_key)]
        location = build_location_name(floor_key, kind, idx)
        by_hostel[hostel_name].append((kind, location))

    return by_hostel
