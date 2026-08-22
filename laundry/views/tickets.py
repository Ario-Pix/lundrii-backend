"""Student ticket list, detail, and raise (multipart photo optional)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from base.permissions import IsStudent
from laundry.models import Ticket, TicketStatus
from laundry.serializers.tickets import (
    StudentTicketCreateSerializer,
    StudentTicketSerializer,
)
from laundry.services.tickets import raise_ticket


class StudentTicketListCreateView(ListAPIView):
    """
    GET /tickets — tickets this student raised (no event thread).
    POST /tickets — multipart: note, machineId, optional photo.
    Allowed while suspended.
    """

    permission_classes = [IsAuthenticated, IsStudent]
    serializer_class = StudentTicketSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        student = self.request.user.student
        qs = Ticket.objects.filter(student=student, is_active=True).select_related(
            "student",
            "machine",
            "machine__hostel",
            "booking",
            "recorded_holder",
        )
        status_filter = (self.request.query_params.get("status") or "").lower()
        if status_filter:
            valid = {c[0] for c in TicketStatus.choices}
            if status_filter not in valid:
                raise ValidationError({"status": "Use open or resolved."})
            qs = qs.filter(status=status_filter)
        return qs

    def post(self, request):
        serializer = StudentTicketCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        ticket = raise_ticket(
            request.user.student,
            kind=data.get("kind") or "maintenance",
            note=data["note"],
            machine_id=data.get("machineId"),
            photo=data.get("photo"),
            actor=request.user,
        )
        return Response(
            StudentTicketSerializer(ticket, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class StudentTicketDetailView(RetrieveAPIView):
    """GET /tickets/{id} — single ticket for the authenticated student (no thread)."""

    permission_classes = [IsAuthenticated, IsStudent]
    serializer_class = StudentTicketSerializer
    lookup_field = "pk"
    lookup_url_kwarg = "ticket_id"

    def get_queryset(self):
        return Ticket.objects.filter(
            student=self.request.user.student,
            is_active=True,
        ).select_related(
            "student",
            "machine",
            "machine__hostel",
            "booking",
            "recorded_holder",
        )

    def get_object(self):
        try:
            return super().get_object()
        except NotFound:
            raise NotFound(detail="Ticket not found.") from None
