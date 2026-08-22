"""Admin analytics aggregates (demand, weekday shape, channel shares)."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from django.db.models import Count
from django.db.models.functions import ExtractHour, ExtractWeekDay
from django.utils import timezone

from laundry.models import AvailabilityMiss, Booking, BookingChannel

# Admin UI labels / colors (aligned to admin/src/data/mock/seed.ts CHANNEL_PALETTE).
CHANNEL_DISPLAY: dict[str, dict[str, str]] = {
    BookingChannel.APP: {"name": "Android app", "color": "#0B8A5B"},
    BookingChannel.ANDROID: {"name": "Android app", "color": "#0B8A5B"},
    BookingChannel.IOS: {"name": "iOS app", "color": "#0E6FA8"},
    BookingChannel.WHATSAPP: {"name": "WhatsApp", "color": "#7A3AA8"},
    BookingChannel.WEBSITE: {"name": "Website", "color": "#A16207"},
    BookingChannel.MCP: {"name": "Assistant (MCP)", "color": "#B45309"},
}

# Django ExtractWeekDay: Sunday=1 … Saturday=7. Admin wants Mon→Sun.
_WEEKDAY_LABELS = (
    (2, "Mon"),
    (3, "Tue"),
    (4, "Wed"),
    (5, "Thu"),
    (6, "Fri"),
    (7, "Sat"),
    (1, "Sun"),
)

_CHANNEL_NOTES = {
    "Android app": "Most residents. Peaks in the evening rush.",
    "iOS app": "Steady share, mostly second-years.",
    "WhatsApp": "Bot bookings. Used late at night and on patchy wifi.",
    "Website": "Falling as the apps take over. Mostly desktop, mostly daytime.",
    "Assistant (MCP)": "Booked by chat through ChatGPT or Claude.",
}


def channel_payload(channel_key: str | None) -> dict[str, str]:
    """Return ``{name, color}`` for a stored channel key."""
    key = (channel_key or BookingChannel.APP).strip().lower()
    meta = CHANNEL_DISPLAY.get(key) or CHANNEL_DISPLAY[BookingChannel.APP]
    return {"name": meta["name"], "color": meta["color"]}


def channel_display_name(channel_key: str | None) -> str:
    return channel_payload(channel_key)["name"]


def resolve_channel_filter(raw: str | None) -> str | None:
    """
    Map query ``channel`` to a stored Booking.channel value, or None for all.

    Accepts storage keys (``app``, ``ios``) or Admin labels (``iOS app``).
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value or value.lower() == "all":
        return None
    lowered = value.lower()
    for key, meta in CHANNEL_DISPLAY.items():
        if lowered == key or lowered == meta["name"].lower():
            return key
    return lowered


def _booking_qs(*, institute_id: UUID | None, hostel_id: UUID | str | None):
    qs = Booking.objects.filter(is_active=True, cancelled_at__isnull=True)
    if institute_id is not None:
        qs = qs.filter(machine__hostel__institute_id=institute_id)
    if hostel_id and str(hostel_id).lower() != "all":
        qs = qs.filter(machine__hostel_id=hostel_id)
    return qs


def _miss_qs(*, institute_id: UUID | None, hostel_id: UUID | str | None):
    qs = AvailabilityMiss.objects.filter(is_active=True)
    if institute_id is not None:
        qs = qs.filter(machine__hostel__institute_id=institute_id)
    if hostel_id and str(hostel_id).lower() != "all":
        qs = qs.filter(machine__hostel_id=hostel_id)
    return qs


def demand_by_hour(
    *,
    institute_id: UUID | None = None,
    hostel_id: UUID | str | None = None,
) -> list[dict]:
    """24 hour points: booked count + turned_away (availability misses)."""
    booked_rows = (
        _booking_qs(institute_id=institute_id, hostel_id=hostel_id)
        .annotate(hour=ExtractHour("starts_at", tzinfo=timezone.get_current_timezone()))
        .values("hour")
        .annotate(n=Count("id"))
    )
    booked = {int(r["hour"]): r["n"] for r in booked_rows if r["hour"] is not None}

    miss_rows = (
        _miss_qs(institute_id=institute_id, hostel_id=hostel_id)
        .values("hour")
        .annotate(n=Count("id"))
    )
    turned = {int(r["hour"]): r["n"] for r in miss_rows}

    return [
        {
            "hour": hour,
            "booked": booked.get(hour, 0),
            "turned_away": turned.get(hour, 0),
        }
        for hour in range(24)
    ]


def weekday_shape(
    *,
    institute_id: UUID | None = None,
    hostel_id: UUID | str | None = None,
) -> list[dict]:
    """Mon→Sun booked + turned_away counts."""
    booked_rows = (
        _booking_qs(institute_id=institute_id, hostel_id=hostel_id)
        .annotate(wd=ExtractWeekDay("starts_at", tzinfo=timezone.get_current_timezone()))
        .values("wd")
        .annotate(n=Count("id"))
    )
    booked = {int(r["wd"]): r["n"] for r in booked_rows if r["wd"] is not None}

    miss_rows = (
        _miss_qs(institute_id=institute_id, hostel_id=hostel_id)
        .annotate(wd=ExtractWeekDay("date"))
        .values("wd")
        .annotate(n=Count("id"))
    )
    # AvailabilityMiss.date is a DateField — ExtractWeekDay works on DateField.
    turned = {int(r["wd"]): r["n"] for r in miss_rows if r["wd"] is not None}

    return [
        {
            "label": label,
            "booked": booked.get(wd, 0),
            "turned_away": turned.get(wd, 0),
        }
        for wd, label in _WEEKDAY_LABELS
    ]


def channel_shares(*, institute_id: UUID | None = None) -> list[dict]:
    """
    Aggregate booking channel shares for the institute (or all for super-admin).

    Until multi-channel clients exist, expect a dominant ``app`` → Android bucket.
    """
    qs = Booking.objects.filter(is_active=True, cancelled_at__isnull=True)
    if institute_id is not None:
        qs = qs.filter(machine__hostel__institute_id=institute_id)

    rows = qs.values("channel").annotate(n=Count("id"))
    # Collapse storage keys that share a display name (app + android → Android app).
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        name = channel_display_name(row["channel"])
        counts[name] += row["n"]

    total = sum(counts.values())
    # Always return every palette entry, so the Admin chart keeps a stable set of
    # buckets. Every display name in CHANNEL_DISPLAY must appear here: a name
    # missing from this list is still counted in `total` but never emitted, which
    # silently drops those bookings and understates every other percentage.
    palette_names = ["Android app", "iOS app", "WhatsApp", "Website", "Assistant (MCP)"]
    colors = {meta["name"]: meta["color"] for meta in CHANNEL_DISPLAY.values()}

    out = []
    for name in palette_names:
        n = counts.get(name, 0)
        pct = round((n / total) * 100) if total else 0
        out.append(
            {
                "name": name,
                "color": colors.get(name, "#0B8A5B"),
                "pct": pct,
                "note": _CHANNEL_NOTES.get(name, ""),
                "trend": "flat vs last week" if total else "no data yet",
                "up": None,
            }
        )
    # Fix rounding so pcts sum to 100 when there is data.
    if total and out:
        drift = 100 - sum(p["pct"] for p in out)
        if drift:
            # Adjust the largest bucket.
            out.sort(key=lambda p: p["pct"], reverse=True)
            out[0]["pct"] += drift
            out.sort(key=lambda p: palette_names.index(p["name"]))
    return out
