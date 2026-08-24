"""Student machine, availability, and booking APIs."""

from __future__ import annotations

from datetime import datetime

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
)
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from base.apidocs import (
    BOOKING_FLOW,
    FAIRNESS_RULES,
    SLOT_STATES,
    error_table,
)
from base.clients import resolve_channel
from base.permissions import IsStudent
from laundry.models import Exchange, ExchangeStatus, Hostel, Machine
from laundry.serializers.student import (
    AvailabilityMissCreateSerializer,
    AvailabilityMissSerializer,
    BookingCreateSerializer,
    BookingMoveSerializer,
    BookingSerializer,
    EligibleHostelSerializer,
    HomeSerializer,
    MachineCardSerializer,
    MeSerializer,
    MoveOptionSerializer,
    SlotSerializer,
)
from laundry.services.booking import (
    BookingRequest,
    cancel_booking,
    create_bookings,
    get_own_booking,
    move_booking,
    move_options,
    past_bookings_qs,
    record_availability_miss,
    upcoming_bookings_qs,
)
from laundry.services.exchanges import expire_stale_pendings
from laundry.services.rules import (
    get_institute_rules,
    machine_is_visible,
    visible_hostels,
)
from laundry.services.slots import (
    SLOT_RUNNING,
    SLOT_TAKEN,
    derive_slots,
    hostel_availability_now,
    machine_live_status,
)


def _student(request):
    return request.user.student


def _optional_student(request):
    """Signed-in student, or None for guest schedule browse."""
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return None
    try:
        student = user.student
    except (ObjectDoesNotExist, AttributeError):
        return None
    if student is None or not getattr(student, "is_active", True):
        return None
    return student


def _parse_date_param(value: str | None, *, default_today: bool = True):
    if not value:
        if default_today:
            return timezone.localdate()
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError({"date": "Use YYYY-MM-DD."}) from exc


def _is_public_browse(student) -> bool:
    """Guests may read occupancy across institutes; students stay institute-scoped."""
    return student is None


def _visible_hostel_or_404(student, hostel_id) -> Hostel:
    try:
        hostel = Hostel.objects.select_related("institute").get(pk=hostel_id, is_active=True)
    except (Hostel.DoesNotExist, ValueError, TypeError) as exc:
        raise NotFound("Hostel not found.") from exc
    if _is_public_browse(student):
        if not hostel.institute.is_active:
            raise NotFound("Hostel not found.")
        return hostel
    if hostel.institute_id != student.institute_id:
        raise NotFound("Hostel not found.")
    return hostel


def _visible_machine_or_404(student, machine_id) -> Machine:
    try:
        machine = Machine.objects.select_related("hostel", "hostel__institute").get(
            pk=machine_id,
            is_active=True,
        )
    except (Machine.DoesNotExist, ValueError, TypeError) as exc:
        raise NotFound("Machine not found.") from exc
    if _is_public_browse(student):
        if not machine.hostel.is_active or not machine.hostel.institute.is_active:
            raise NotFound("Machine not found.")
        return machine
    if not machine_is_visible(student, machine):
        raise NotFound("Machine not found.")
    return machine


def _slot_payload(slot, *, student) -> dict:
    data = SlotSerializer.from_slot(slot)
    if student is None:
        # Occupancy stays; holder identity is signed-in only.
        data["holder"] = None
        data["bookingId"] = None
        if data["state"] == SLOT_TAKEN:
            data["label"] = "Taken"
        elif data["state"] == SLOT_RUNNING:
            data["label"] = "Running now"
    return data


def _item_result_payload(result, request) -> dict:
    payload = {
        "ok": result.ok,
        "machineId": result.machine_id,
    }
    if result.ok and result.booking is not None:
        payload["booking"] = BookingSerializer(
            result.booking, context={"request": request}
        ).data
        return payload
    payload["code"] = result.code
    payload["detail"] = result.detail
    if result.rule:
        payload["rule"] = result.rule
    payload["clearsAt"] = result.clears_at.isoformat() if result.clears_at else None
    return payload


class StudentAPIMixin:
    permission_classes = [IsAuthenticated, IsStudent]


class BrowseScheduleMixin:
    """Read-only machines/slots/availability. Guests see occupancy; students keep personal states."""

    permission_classes = [AllowAny]


def _resolve_home_hostel(student, requested_hostel_id: str | None) -> Hostel | None:
    """Pick selected hostel: optional query → home → first eligible. Bad id → 404."""
    if student is not None:
        hostels = list(visible_hostels(student).order_by("name"))
    else:
        hostels = list(
            Hostel.objects.filter(is_active=True, institute__is_active=True)
            .select_related("institute")
            .order_by("institute__name", "name")
        )

    if requested_hostel_id:
        return _visible_hostel_or_404(student, requested_hostel_id)

    if not hostels:
        return None
    if student is not None and student.home_hostel_id:
        for hostel in hostels:
            if hostel.id == student.home_hostel_id:
                return hostel
    return hostels[0]


def _home_hostel_payload(student) -> list[dict]:
    if student is not None:
        hostels = visible_hostels(student).order_by("name")
        return [EligibleHostelSerializer.from_hostel(h, student) for h in hostels]
    hostels = (
        Hostel.objects.filter(is_active=True, institute__is_active=True)
        .select_related("institute")
        .order_by("institute__name", "name")
    )
    return [EligibleHostelSerializer.public(h) for h in hostels]


def _pending_incoming_exchange_count(student) -> int:
    expire_stale_pendings(student=student)
    return Exchange.objects.filter(
        holder=student,
        status=ExchangeStatus.PENDING,
    ).count()


class HomeView(BrowseScheduleMixin, APIView):
    """One call for Home: profile (if JWT), hostels, machines, washer counts, upcoming."""

    pagination_class = None
    serializer_class = HomeSerializer

    @extend_schema(
        summary="Home bootstrap",
        description=(
            "Single payload for the Home screen. Guests get public hostels and "
            "live machine cards; a student JWT adds profile, upcoming bookings "
            "(max 2), and pending incoming exchange count.\n\n"
            "Optional `hostelId` selects the hostel (same visibility rules as "
            "availability/now). Omit it to use home hostel, then first eligible."
        ),
        parameters=[
            OpenApiParameter(
                "hostelId",
                str,
                description="Hostel to show machines and washer counts for.",
            )
        ],
        responses=HomeSerializer,
    )
    def get(self, request):
        student = _optional_student(request)
        requested = (request.query_params.get("hostelId") or "").strip() or None
        selected = _resolve_home_hostel(student, requested)

        hostels = _home_hostel_payload(student)
        profile = MeSerializer.from_student(student) if student else None
        upcoming: list = []
        pending = 0
        if student is not None:
            bookings = list(upcoming_bookings_qs(student)[:2])
            upcoming = BookingSerializer(
                bookings, many=True, context={"request": request}
            ).data
            pending = _pending_incoming_exchange_count(student)

        if selected is None:
            return Response(
                {
                    "profile": profile,
                    "hostels": hostels,
                    "selectedHostelId": None,
                    "machines": [],
                    "washersFree": 0,
                    "washersTotal": 0,
                    "upcoming": upcoming,
                    "pendingIncomingExchangeCount": pending,
                }
            )

        snap = hostel_availability_now(selected, student=student)
        washers = snap["washers"]
        return Response(
            {
                "profile": profile,
                "hostels": hostels,
                "selectedHostelId": selected.id,
                "machines": [
                    MachineCardSerializer.from_machine(row["machine"], row)
                    for row in snap["machines"]
                ],
                "washersFree": washers["free_now"],
                "washersTotal": washers["total"],
                "upcoming": upcoming,
                "pendingIncomingExchangeCount": pending,
            }
        )


class HostelMachineListView(BrowseScheduleMixin, ListAPIView):
    serializer_class = MachineCardSerializer

    def get_queryset(self):
        student = _optional_student(self.request)
        hostel = _visible_hostel_or_404(student, self.kwargs["hostel_id"])
        qs = Machine.objects.filter(hostel=hostel, is_active=True).select_related(
            "hostel", "hostel__institute"
        )
        kind = self.request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        return qs

    def list(self, request, *args, **kwargs):
        student = _optional_student(request)
        machines = list(self.filter_queryset(self.get_queryset()))
        hostel = machines[0].hostel if machines else _visible_hostel_or_404(
            student, self.kwargs["hostel_id"]
        )
        rules = get_institute_rules(hostel.institute)
        now = timezone.now()
        results = []
        for machine in machines:
            live = machine_live_status(machine, now=now, student=student, rules=rules)
            results.append(MachineCardSerializer.from_machine(machine, live))
        page = self.paginate_queryset(results)
        if page is not None:
            return self.get_paginated_response(page)
        return Response(results)


class MachineDetailView(BrowseScheduleMixin, APIView):
    serializer_class = MachineCardSerializer

    def get(self, request, machine_id):
        student = _optional_student(request)
        machine = _visible_machine_or_404(student, machine_id)
        rules = get_institute_rules(machine.hostel.institute)
        live = machine_live_status(machine, student=student, rules=rules)
        return Response(MachineCardSerializer.from_machine(machine, live))


class MachineSlotListView(BrowseScheduleMixin, APIView):
    """Day slot grid — pagination explicitly disabled."""

    pagination_class = None
    serializer_class = SlotSerializer

    @extend_schema(
        summary="Slots for one machine on one day",
        description=(
            "The full day's derived slots for a machine, with each slot's state "
            "from **this** student's point of view.\n\n"
            + SLOT_STATES
            + "\nSlots have no id. To book one, send its `machineId` plus "
            "either `startsAt` or `date` + `hour` to `POST /bookings`.\n\n"
            + error_table("NOT_FOUND", "VALIDATION_ERROR")
        ),
        parameters=[
            OpenApiParameter(
                "date",
                str,
                description="Day to show, YYYY-MM-DD. Defaults to today.",
                examples=[OpenApiExample("Tomorrow", value="2026-08-19")],
            )
        ],
        examples=[
            OpenApiExample(
                "A morning with one slot already taken",
                value={
                    "date": "2026-08-19",
                    "machineId": "5f51b742-80fb-47f8-bbb7-c572d1d2c16b",
                    "machineName": "3rd Floor · A Wing",
                    "kind": "washer",
                    "isOffline": False,
                    "slotLengthMinutes": 60,
                    "slots": [
                        {
                            "startsAt": "2026-08-19T08:00:00+05:30",
                            "endsAt": "2026-08-19T09:00:00+05:30",
                            "hour": 8,
                            "state": "free",
                            "label": "Available",
                        },
                        {
                            "startsAt": "2026-08-19T09:00:00+05:30",
                            "endsAt": "2026-08-19T10:00:00+05:30",
                            "hour": 9,
                            "state": "taken",
                            "label": "Riya Sharma",
                        },
                        {
                            "startsAt": "2026-08-19T10:00:00+05:30",
                            "endsAt": "2026-08-19T11:00:00+05:30",
                            "hour": 10,
                            "state": "blocked",
                            "label": "Quota",
                            "blockedRule": "quota",
                            "clearsAt": "2026-08-21T10:00:00+05:30",
                        },
                    ],
                },
                response_only=True,
            )
        ],
    )
    def get(self, request, machine_id):
        student = _optional_student(request)
        machine = _visible_machine_or_404(student, machine_id)
        on_date = _parse_date_param(request.query_params.get("date"))
        slots = derive_slots(machine, on_date, student=student)
        return Response(
            {
                "date": on_date.isoformat(),
                "machineId": str(machine.id),
                "machineName": machine.location_name,
                "kind": machine.kind,
                "isOffline": machine.is_offline,
                "slotLengthMinutes": machine.slot_length_minutes,
                "slots": [_slot_payload(s, student=student) for s in slots],
            }
        )


class HostelAvailabilityNowView(BrowseScheduleMixin, APIView):
    pagination_class = None
    serializer_class = MachineCardSerializer

    def get(self, request, hostel_id):
        student = _optional_student(request)
        hostel = _visible_hostel_or_404(student, hostel_id)
        snap = hostel_availability_now(hostel, student=student)

        def kind_payload(group: dict) -> dict:
            machine = group["next_free_machine"]
            return {
                "freeNow": group["free_now"],
                "total": group["total"],
                "nextFreeAt": group["next_free_at"],
                "nextFreeMachineId": machine.id if machine else None,
                "nextFreeMachineName": machine.location_name if machine else None,
                "freeingSoon": [
                    {
                        "machineId": row["machine"].id,
                        "machineName": row["machine"].location_name,
                        "at": row["at"],
                    }
                    for row in group["freeing_soon"]
                ],
            }

        return Response(
            {
                "asOf": snap["as_of"],
                "hostelId": str(hostel.id),
                "hostelName": hostel.name,
                "washers": kind_payload(snap["washers"]),
                "dryers": kind_payload(snap["dryers"]),
                "machines": [
                    MachineCardSerializer.from_machine(row["machine"], row)
                    for row in snap["machines"]
                ],
            }
        )


class AvailabilityMissCreateView(StudentAPIMixin, APIView):
    serializer_class = AvailabilityMissCreateSerializer

    def post(self, request):
        student = _student(request)
        serializer = AvailabilityMissCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        machine = _visible_machine_or_404(student, data["machineId"])
        miss = record_availability_miss(
            student,
            machine,
            data["date"],
            data["hour"],
        )
        return Response(
            AvailabilityMissSerializer(miss).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema(
    summary="List bookings",
    description=(
        "This student's bookings. `status=upcoming` (default) returns bookings "
        "that have not finished; `status=past` returns finished and cancelled "
        "ones.\n\n" + error_table("VALIDATION_ERROR")
    ),
    parameters=[
        OpenApiParameter(
            "status",
            str,
            enum=["upcoming", "past"],
            description="Which half of the history to return. Default `upcoming`.",
        )
    ],
)
class BookingListCreateView(StudentAPIMixin, ListAPIView):
    serializer_class = BookingSerializer

    def get_queryset(self):
        student = _student(self.request)
        status_filter = (self.request.query_params.get("status") or "upcoming").lower()
        if status_filter == "upcoming":
            return upcoming_bookings_qs(student)
        if status_filter == "past":
            return past_bookings_qs(student)
        raise ValidationError({"status": "Use upcoming or past."})

    @extend_schema(
        summary="Book one or more slots",
        description=(
            "Claim slots. A washer and a dryer in one request are two "
            "**independent** bookings: each is attempted on its own and either "
            "can fail while the other succeeds, so always read the per-item "
            "`results` rather than relying on the HTTP status. The response is "
            "`200` even when every item failed.\n\n"
            "Identify each slot by `machineId` plus either `startsAt` or "
            "`date` + `hour`. Slots are first-come, so a slot that was free "
            "when you listed it can still come back `SLOT_TAKEN`.\n\n"
            + FAIRNESS_RULES
            + "\n`UNVERIFIED` and `SUSPENDED` reject the whole request rather "
            "than individual items.\n\n"
            + error_table(
                "VALIDATION_ERROR",
                "RULE_BLOCKED",
                "SLOT_TAKEN",
                "MACHINE_OFFLINE",
                "PAST_SLOT",
                "OUTSIDE_ADVANCE_WINDOW",
                "UNVERIFIED",
                "SUSPENDED",
                "NOT_FOUND",
            )
        ),
        request=BookingCreateSerializer,
        examples=[
            OpenApiExample(
                "Washer and dryer back to back",
                value={
                    "items": [
                        {
                            "machineId": "5f51b742-80fb-47f8-bbb7-c572d1d2c16b",
                            "date": "2026-08-19",
                            "hour": 20,
                        },
                        {
                            "machineId": "e12dff97-6a07-4e24-9402-b2b0e61c2b6e",
                            "date": "2026-08-19",
                            "hour": 21,
                        },
                    ]
                },
                request_only=True,
            ),
            OpenApiExample(
                "Exact start time instead of date + hour",
                value={
                    "items": [
                        {
                            "machineId": "5f51b742-80fb-47f8-bbb7-c572d1d2c16b",
                            "startsAt": "2026-08-19T20:00:00+05:30",
                        }
                    ]
                },
                request_only=True,
            ),
            OpenApiExample(
                "Partial success — washer booked, dryer blocked by quota",
                description=(
                    "HTTP 200. The first item succeeded and the second did not; "
                    "this is why per-item results exist."
                ),
                value={
                    "results": [
                        {
                            "ok": True,
                            "index": 0,
                            "machineId": "5f51b742-80fb-47f8-bbb7-c572d1d2c16b",
                            "booking": {
                                "id": "98a87976-fd32-43ed-8d9a-b37cf57823f4",
                                "startsAt": "2026-08-19T20:00:00+05:30",
                                "endsAt": "2026-08-19T21:00:00+05:30",
                            },
                        },
                        {
                            "ok": False,
                            "index": 1,
                            "machineId": "e12dff97-6a07-4e24-9402-b2b0e61c2b6e",
                            "code": "RULE_BLOCKED",
                            "detail": "You have used all 3 bookings this week.",
                            "rule": "quota",
                            "clearsAt": "2026-08-22T09:00:00Z",
                        },
                    ]
                },
                response_only=True,
            ),
        ],
    )
    def post(self, request):
        student = _student(request)
        serializer = BookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requests = [
            BookingRequest(
                machine_id=item["machineId"],
                starts_at=item.get("startsAt"),
                date=item.get("date"),
                hour=item.get("hour"),
            )
            for item in serializer.validated_data["items"]
        ]
        results = create_bookings(student, requests, channel=resolve_channel(request))
        payload = {
            "results": [
                {**_item_result_payload(item, request), "index": index}
                for index, item in enumerate(results)
            ]
        }
        return Response(payload, status=status.HTTP_200_OK)


class BookingDetailView(StudentAPIMixin, APIView):
    serializer_class = BookingSerializer

    def get(self, request, booking_id):
        student = _student(request)
        booking = get_own_booking(student, booking_id)
        return Response(BookingSerializer(booking, context={"request": request}).data)


@extend_schema(
    summary="Cancel a booking",
    description=(
        "Cancel an upcoming booking. Cancelling nearer the start than the "
        "institute's cancellation cutoff is a *late* cancellation: the slot is "
        "released, but it still counts against the student's quota and is "
        "recorded as such. A booking that has already started cannot be "
        "cancelled.\n\n" + error_table("NOT_FOUND", "PAST_SLOT", "SUSPENDED")
    ),
    request=None,
)
class BookingCancelView(StudentAPIMixin, APIView):
    serializer_class = BookingSerializer

    def post(self, request, booking_id):
        student = _student(request)
        booking = get_own_booking(student, booking_id)
        booking = cancel_booking(student, booking)
        return Response(BookingSerializer(booking, context={"request": request}).data)


class BookingMoveView(StudentAPIMixin, APIView):
    serializer_class = BookingMoveSerializer

    def post(self, request, booking_id):
        student = _student(request)
        booking = get_own_booking(student, booking_id)
        serializer = BookingMoveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        booking = move_booking(
            student,
            booking,
            BookingRequest(
                machine_id=data.get("machineId") or booking.machine_id,
                starts_at=data.get("startsAt"),
                date=data.get("date"),
                hour=data.get("hour"),
            ),
        )
        return Response(BookingSerializer(booking, context={"request": request}).data)


class BookingMoveOptionsView(StudentAPIMixin, APIView):
    pagination_class = None
    serializer_class = MoveOptionSerializer

    def get(self, request, booking_id):
        student = _student(request)
        booking = get_own_booking(student, booking_id)
        options = move_options(student, booking)
        return Response(
            {
                "bookingId": str(booking.id),
                "options": [MoveOptionSerializer.from_option(row) for row in options],
            }
        )
