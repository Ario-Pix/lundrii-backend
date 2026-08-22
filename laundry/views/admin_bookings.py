"""Admin booking grid, detail, cancel, and day CSV export."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta
from uuid import UUID

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from base.exceptions import APIError
from base.permissions import IsAdministratorOrSuperAdministrator
from laundry.models import Booking, Machine, NotificationKind, NotificationType
from laundry.permissions import scoped_institute_id
from laundry.serializers.admin_bookings import (
    BookingCellSerializer,
    BookingDetailSerializer,
    ChannelShareSerializer,
    DemandHourPointSerializer,
    WeekdayPointSerializer,
)
from laundry.services.analytics import (
    channel_display_name,
    channel_payload,
    channel_shares,
    demand_by_hour,
    resolve_channel_filter,
    weekday_shape,
)
from laundry.services.notifications import create_in_app_notification
from laundry.services.slots import iter_operating_slots

GRID_HOURS = tuple(range(6, 24))


def _parse_date(raw: str | None) -> date:
    if not raw:
        return timezone.localdate()
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise APIError(
            "VALIDATION_ERROR",
            detail="Invalid date. Use YYYY-MM-DD.",
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from exc


def _aware(dt: datetime) -> datetime:
    if timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt, timezone.get_current_timezone())


def _local(dt: datetime) -> datetime:
    return timezone.localtime(_aware(dt))


def _day_bounds(on_date: date) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(on_date, time.min), tz)
    end = start + timedelta(days=1)
    return start, end


def _cell_state(booking: Booking | None, *, now: datetime, closed: bool) -> str:
    if closed:
        return "closed"
    if booking is None:
        return "open"
    starts = _aware(booking.starts_at)
    ends = _aware(booking.ends_at)
    if ends <= now:
        return "completed"
    if starts <= now < ends:
        return "running"
    return "upcoming"


def _slot_label(hour: int) -> str:
    return f"{hour:02d}:00 – {hour + 1:02d}:00"


def _booked_at_label(created_at: datetime, *, now: datetime) -> str:
    created = _local(created_at)
    now_local = _local(now)
    delta_days = (now_local.date() - created.date()).days
    clock = created.strftime("%H:%M")
    if delta_days <= 0:
        day = "today"
    elif delta_days == 1:
        day = "yesterday"
    else:
        day = f"{delta_days} days ago"
    return f"{day} at {clock}"


def _hour_is_operable(machine: Machine, on_date: date, hour: int) -> bool:
    """True when an operating slot starts at ``hour`` on ``on_date``."""
    for start, _end in iter_operating_slots(machine, on_date):
        if _local(start).hour == hour:
            return True
    return False


def _scoped_machines(user, hostel_id: str | None):
    qs = Machine.objects.filter(is_active=True).select_related("hostel")
    institute_id = scoped_institute_id(user)
    if institute_id is not None:
        qs = qs.filter(hostel__institute_id=institute_id)
    if hostel_id and hostel_id.lower() != "all":
        qs = qs.filter(hostel_id=hostel_id)
    return list(qs.order_by("hostel__name", "kind", "location_name"))


def _scoped_booking_qs(user):
    qs = Booking.objects.select_related(
        "student",
        "student__user",
        "machine",
        "machine__hostel",
    )
    institute_id = scoped_institute_id(user)
    if institute_id is not None:
        qs = qs.filter(machine__hostel__institute_id=institute_id)
    return qs


def build_booking_grid(
    *,
    user,
    on_date: date,
    hostel_id: str | None,
    channel_filter: str | None,
    now: datetime | None = None,
) -> list[dict]:
    """
    Build BookingCell-shaped rows for hour×machine admin grid.

    ``channel`` filter is accepted for API parity with the Admin client; cells
    are still returned for all hours so the grid layout stays intact (UI dims).
    """
    now = _aware(now or timezone.now())
    machines = _scoped_machines(user, hostel_id)
    day_start, day_end = _day_bounds(on_date)
    date_label = on_date.isoformat()

    bookings = list(
        _scoped_booking_qs(user).filter(
            machine_id__in=[m.id for m in machines],
            cancelled_at__isnull=True,
            is_active=True,
            starts_at__gte=day_start,
            starts_at__lt=day_end,
        )
    )
    by_machine_hour: dict[tuple, Booking] = {}
    for booking in bookings:
        hour = _local(booking.starts_at).hour
        by_machine_hour[(booking.machine_id, hour)] = booking

    # Touch channel_filter so unused-arg linters stay quiet; layout keeps all cells.
    _ = channel_filter

    cells: list[dict] = []
    for machine in machines:
        for hour in GRID_HOURS:
            closed = machine.is_offline or not _hour_is_operable(machine, on_date, hour)
            booking = None if closed else by_machine_hour.get((machine.id, hour))
            state = _cell_state(booking, now=now, closed=closed)
            slot = _slot_label(hour)

            if closed:
                tip = (
                    f"{machine.hostel.name} · {machine.location_name} is offline"
                    if machine.is_offline
                    else "Outside operating hours"
                )
                cells.append(
                    {
                        "machine_id": str(machine.id),
                        "machine_label": machine.location_name,
                        "hostel_name": machine.hostel.name,
                        "hour": hour,
                        "date": date_label,
                        "state": state,
                        "student_name": None,
                        "student_id": None,
                        "channel": None,
                        "slot_label": slot,
                        "tip": tip,
                    }
                )
                continue

            if booking is None:
                cells.append(
                    {
                        "machine_id": str(machine.id),
                        "machine_label": machine.location_name,
                        "hostel_name": machine.hostel.name,
                        "hour": hour,
                        "date": date_label,
                        "state": "open",
                        "student_name": None,
                        "student_id": None,
                        "channel": None,
                        "slot_label": slot,
                        "tip": f"{hour:02d}:00 free",
                    }
                )
                continue

            channel = channel_payload(booking.channel)
            cells.append(
                {
                    "machine_id": str(machine.id),
                    "machine_label": machine.location_name,
                    "hostel_name": machine.hostel.name,
                    "hour": hour,
                    "date": date_label,
                    "state": state,
                    "student_name": booking.student.name,
                    "student_id": str(booking.student_id),
                    "channel": channel,
                    "slot_label": slot,
                    "tip": f"{booking.student.name} · {channel['name']}",
                    "booking_id": str(booking.id),
                }
            )
    return cells


def serialize_booking_detail(booking: Booking, *, now: datetime | None = None) -> dict:
    now = _aware(now or timezone.now())
    closed = booking.machine.is_offline
    state = _cell_state(
        None if booking.cancelled_at else booking,
        now=now,
        closed=closed and booking.cancelled_at is None,
    )
    if booking.cancelled_at is not None:
        # Cancelled bookings surface as completed for grid semantics.
        state = "completed"
    return {
        "id": str(booking.id),
        "student_name": booking.student.name,
        "student_id": str(booking.student_id),
        "machine_id": str(booking.machine_id),
        "machine_label": booking.machine.location_name,
        "hostel_name": booking.machine.hostel.name,
        "starts_at": _aware(booking.starts_at).isoformat(),
        "ends_at": _aware(booking.ends_at).isoformat(),
        "channel": channel_payload(booking.channel),
        "booked_at_label": _booked_at_label(booking.created_at, now=now),
        "state": state,
        "cancelled_at": (
            _aware(booking.cancelled_at).isoformat() if booking.cancelled_at else None
        ),
    }


def admin_cancel_booking(booking: Booking, *, now: datetime | None = None) -> Booking:
    """Committee cancel: release quota, notify student. Allows future slots only."""
    now = _aware(now or timezone.now())
    if booking.cancelled_at is not None:
        return booking
    if booking.starts_at <= now:
        raise APIError(
            "PAST_SLOT",
            detail="Cannot cancel a booking that has already started.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    with transaction.atomic():
        booking.cancelled_at = now
        booking.is_late_cancel = False
        booking.counts_against_quota = False
        booking.save(
            update_fields=[
                "cancelled_at",
                "is_late_cancel",
                "counts_against_quota",
                "updated_at",
            ]
        )
        when = _local(booking.starts_at).strftime("%Y-%m-%d %H:%M")
        create_in_app_notification(
            student=booking.student,
            title="Booking cancelled",
            body=(
                f"Your booking for {booking.machine.location_name} on {when} "
                "was cancelled by the laundry committee."
            ),
            notification_type=NotificationType.BOOKING_CANCELLED_OFFLINE,
            kind=NotificationKind.WARN,
            related_object_type="booking",
            related_object_id=booking.id,
            preference_field="booking_cancelled_offline",
        )
    return booking


def build_day_csv(
    *,
    user,
    on_date: date,
    hostel_id: str | None,
) -> str:
    machines = _scoped_machines(user, hostel_id)
    day_start, day_end = _day_bounds(on_date)
    bookings = (
        _scoped_booking_qs(user)
        .filter(
            machine_id__in=[m.id for m in machines],
            starts_at__gte=day_start,
            starts_at__lt=day_end,
        )
        .order_by("starts_at", "machine__location_name")
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "date",
            "hour",
            "machine",
            "hostel",
            "student",
            "student_email",
            "channel",
            "starts_at",
            "ends_at",
            "cancelled_at",
            "status",
        ]
    )
    for booking in bookings:
        local_start = _local(booking.starts_at)
        status_label = "cancelled" if booking.cancelled_at else "active"
        writer.writerow(
            [
                on_date.isoformat(),
                local_start.hour,
                booking.machine.location_name,
                booking.machine.hostel.name,
                booking.student.name,
                booking.student.user.email,
                channel_display_name(booking.channel),
                _aware(booking.starts_at).isoformat(),
                _aware(booking.ends_at).isoformat(),
                (
                    _aware(booking.cancelled_at).isoformat()
                    if booking.cancelled_at
                    else ""
                ),
                status_label,
            ]
        )
    return buf.getvalue()


class AdminBookingGridView(APIView):
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]

    @extend_schema(
        parameters=[
            OpenApiParameter("date", str, description="YYYY-MM-DD (defaults to today)"),
            OpenApiParameter("hostel", str, description="Hostel UUID or 'all'"),
            OpenApiParameter("channel", str, description="Channel key/label or 'all'"),
        ],
        responses=BookingCellSerializer(many=True),
    )
    def get(self, request):
        on_date = _parse_date(request.query_params.get("date"))
        hostel = request.query_params.get("hostel")
        channel = resolve_channel_filter(request.query_params.get("channel"))
        cells = build_booking_grid(
            user=request.user,
            on_date=on_date,
            hostel_id=hostel,
            channel_filter=channel,
        )
        return Response(cells)


class AdminBookingDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]

    @extend_schema(responses=BookingDetailSerializer)
    def get(self, request, booking_id: UUID):
        booking = get_object_or_404(_scoped_booking_qs(request.user), pk=booking_id)
        return Response(serialize_booking_detail(booking))


class AdminBookingCancelView(APIView):
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]

    # request=None: the booking is identified by the URL, there is no body.
    # Without it drf-spectacular cannot guess a request serializer and drops
    # the whole endpoint from the schema.
    @extend_schema(request=None, responses=BookingDetailSerializer)
    def post(self, request, booking_id: UUID):
        booking = get_object_or_404(_scoped_booking_qs(request.user), pk=booking_id)
        booking = admin_cancel_booking(booking)
        return Response(serialize_booking_detail(booking))


class AdminBookingExportCsvView(APIView):
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]

    # This endpoint returns CSV, not JSON, so the response has to be declared
    # against its media type — drf-spectacular has no serializer to infer from
    # and would otherwise drop the endpoint from the schema.
    @extend_schema(
        parameters=[
            OpenApiParameter("date", str, description="YYYY-MM-DD (defaults to today)"),
            OpenApiParameter("hostel", str, description="Hostel UUID or 'all'"),
        ],
        responses={
            (200, "text/csv"): OpenApiResponse(
                response=OpenApiTypes.STR,
                description="Day's bookings as a CSV attachment.",
            )
        },
    )
    def get(self, request):
        on_date = _parse_date(request.query_params.get("date"))
        hostel = request.query_params.get("hostel")
        body = build_day_csv(user=request.user, on_date=on_date, hostel_id=hostel)
        response = HttpResponse(body, content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="lundrii-bookings-{on_date.isoformat()}.csv"'
        )
        return response


class AdminDemandByHourView(APIView):
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]

    @extend_schema(
        parameters=[OpenApiParameter("hostel", str)],
        responses=DemandHourPointSerializer(many=True),
    )
    def get(self, request):
        institute_id = scoped_institute_id(request.user)
        hostel = request.query_params.get("hostel")
        data = demand_by_hour(institute_id=institute_id, hostel_id=hostel)
        return Response(data)


class AdminWeekdayShapeView(APIView):
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]

    @extend_schema(
        parameters=[OpenApiParameter("hostel", str)],
        responses=WeekdayPointSerializer(many=True),
    )
    def get(self, request):
        institute_id = scoped_institute_id(request.user)
        hostel = request.query_params.get("hostel")
        data = weekday_shape(institute_id=institute_id, hostel_id=hostel)
        return Response(data)


class AdminChannelSharesView(APIView):
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]

    @extend_schema(responses=ChannelShareSerializer(many=True))
    def get(self, request):
        institute_id = scoped_institute_id(request.user)
        data = channel_shares(institute_id=institute_id)
        return Response(data)
