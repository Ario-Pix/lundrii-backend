"""Student exchange request / swap APIs."""

from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from base.permissions import IsStudent
from laundry.permissions import IsStudentCanMutate
from laundry.serializers.exchanges import (
    ExchangeCreateSerializer,
    ExchangeRejectSerializer,
    ExchangeSerializer,
)
from laundry.services.exchanges import (
    approve_exchange,
    create_exchange,
    exchanges_qs,
    get_exchange_for_student,
    reject_exchange,
    withdraw_exchange,
)


def _student(request):
    return request.user.student


class StudentAPIMixin:
    permission_classes = [IsAuthenticated, IsStudent]


class ExchangeListCreateView(StudentAPIMixin, ListAPIView):
    # GET stays readable while suspended; POST runs assert_can_mutate.
    permission_classes = [IsAuthenticated, IsStudentCanMutate]
    serializer_class = ExchangeSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["student"] = _student(self.request)
        return ctx

    def get_queryset(self):
        student = _student(self.request)
        direction = (self.request.query_params.get("direction") or "").strip().lower()
        if direction and direction not in ("incoming", "outgoing"):
            raise ValidationError({"direction": "Use incoming or outgoing."})
        return exchanges_qs(student, direction=direction or None)

    def post(self, request):
        student = _student(request)
        serializer = ExchangeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        exchange = create_exchange(
            student,
            kind=data["kind"],
            target_booking_id=data["target_booking_id"],
            offered_booking_id=data.get("offered_booking_id"),
        )
        return Response(
            ExchangeSerializer(exchange, context={"request": request, "student": student}).data,
            status=status.HTTP_201_CREATED,
        )


class ExchangeApproveView(StudentAPIMixin, APIView):
    permission_classes = [IsAuthenticated, IsStudentCanMutate]
    serializer_class = ExchangeSerializer

    def post(self, request, exchange_id):
        student = _student(request)
        exchange = get_exchange_for_student(student, exchange_id)
        exchange = approve_exchange(student, exchange)
        return Response(
            ExchangeSerializer(exchange, context={"request": request, "student": student}).data
        )


class ExchangeRejectView(StudentAPIMixin, APIView):
    serializer_class = ExchangeSerializer

    def post(self, request, exchange_id):
        student = _student(request)
        exchange = get_exchange_for_student(student, exchange_id)
        serializer = ExchangeRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        exchange = reject_exchange(
            student,
            exchange,
            note=serializer.validated_data.get("note", ""),
        )
        return Response(
            ExchangeSerializer(exchange, context={"request": request, "student": student}).data
        )


class ExchangeWithdrawView(StudentAPIMixin, APIView):
    serializer_class = ExchangeSerializer

    def post(self, request, exchange_id):
        student = _student(request)
        exchange = get_exchange_for_student(student, exchange_id)
        exchange = withdraw_exchange(student, exchange)
        return Response(
            ExchangeSerializer(exchange, context={"request": request, "student": student}).data
        )
