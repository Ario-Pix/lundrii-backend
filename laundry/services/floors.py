"""Floor labels derived from machine locations (e.g. "3rd Floor · A Wing")."""

from __future__ import annotations

import re

from laundry.models import Hostel, Machine

DEFAULT_FLOORS = (
    "Ground Floor",
    "1st Floor",
    "2nd Floor",
    "3rd Floor",
    "4th Floor",
    "5th Floor",
    "6th Floor",
    "7th Floor",
)

_FLOOR_NUMBER = re.compile(r"(\d+)")


def floor_from_location(location_name: str) -> str:
    name = (location_name or "").strip()
    if " · " in name:
        return name.split(" · ", 1)[0].strip()
    return name or DEFAULT_FLOORS[0]


def _rank(label: str) -> tuple:
    lowered = label.lower()
    if lowered.startswith("ground"):
        return (0, 0, label)
    match = _FLOOR_NUMBER.search(label)
    if match:
        return (1, int(match.group(1)), label)
    return (2, 0, label)


def floors_for_hostel(hostel: Hostel) -> list[str]:
    locations = Machine.objects.filter(hostel=hostel, is_active=True).values_list(
        "location_name", flat=True
    )
    seen: set[str] = set()
    floors: list[str] = []
    for location in locations:
        label = floor_from_location(location)
        if label and label not in seen:
            seen.add(label)
            floors.append(label)
    if not floors:
        floors = list(DEFAULT_FLOORS)
    floors.sort(key=_rank)
    return floors
