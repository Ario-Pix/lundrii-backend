"""Floor labels for sign-up / profile.

Machine locations are often "3rd Floor · A Wing". Production hostels sometimes
name machines "Washing Machine" / "Dryer Machine" instead — those are not floors.
"""

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
_FLOOR_HEAD = re.compile(
    r"(?ix)"
    r"^\s*"
    r"(?P<label>"
    r"ground(?:\s+floor)?"
    r"|gf"
    r"|(?:\d+)(?:st|nd|rd|th)\s+floor"
    r"|floor\s+\d+"
    r")"
    r"\b"
)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _canonical_floor(raw: str) -> str:
    lowered = raw.lower().strip()
    if lowered in ("ground", "ground floor", "gf"):
        return "Ground Floor"
    numbered = re.match(
        r"(?:floor\s+)?(\d+)(?:st|nd|rd|th)?(?:\s+floor)?$",
        lowered,
    )
    if numbered:
        n = int(numbered.group(1))
        if n == 0:
            return "Ground Floor"
        return f"{_ordinal(n)} Floor"
    return raw.strip()


def floor_from_location(location_name: str) -> str | None:
    """Return a floor label, or None if the location is not a floor (e.g. a machine name)."""
    name = (location_name or "").strip()
    if not name:
        return None
    head = name.split(" · ", 1)[0].strip()
    match = _FLOOR_HEAD.match(head)
    if not match:
        return None
    return _canonical_floor(match.group("label"))


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
