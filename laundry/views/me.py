"""Student profile, institute, hostels, and notification APIs."""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from base.apidocs import BOOKING_FLOW, FAIRNESS_RULES, error_table
from base.permissions import IsStudent
from laundry.models import Notification
from laundry.serializers.student import (
    EligibleHostelSerializer,
    MeInstituteSerializer,
    MeSerializer,
    MeUpdateSerializer,
    NotificationPreferenceSerializer,
    NotificationSerializer,
)
from laundry.services.notifications import (
    get_or_create_preferences,
    mark_all_notifications_read,
    mark_notification_read,
)
from laundry.services.rules import visible_hostels


def _student(request):
    return request.user.student


class StudentAPIMixin:
    permission_classes = [IsAuthenticated, IsStudent]


@extend_schema(
    summary="The signed-in student",
    description=(
        "Profile, verification and suspension state, current quota usage, and "
        "any active strikes.\n\n" + BOOKING_FLOW
    ),
)
class MeView(StudentAPIMixin, APIView):
    serializer_class = MeSerializer

    def get(self, request):
        return Response(MeSerializer.from_student(_student(request)))

    def patch(self, request):
        student = _student(request)
        serializer = MeUpdateSerializer(student, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        student.refresh_from_db()
        return Response(MeSerializer.from_student(student))


class MeHostelsView(StudentAPIMixin, APIView):
    pagination_class = None
    serializer_class = EligibleHostelSerializer

    def get(self, request):
        student = _student(request)
        hostels = visible_hostels(student).order_by("name")
        return Response(
            [EligibleHostelSerializer.from_hostel(h, student) for h in hostels]
        )


@extend_schema(
    summary="The fairness rules that apply to this student",
    description=(
        "The institute's booking rules as they apply to the caller. Read this "
        "before booking so the UI can explain a refusal before the server has "
        "to.\n\n" + FAIRNESS_RULES
    ),
    examples=[
        OpenApiExample(
            "Three washes a week",
            value={
                "instituteName": "Goa Institute of Management",
                "quotaLimit": 3,
                "quotaWindowDays": 7,
                "cooldownHours": 0,
                "advanceWindowDays": 7,
                "cancellationCutoffHours": 6,
                "dryerCapEnabled": True,
            },
            response_only=True,
        )
    ],
)
class MeInstituteView(StudentAPIMixin, APIView):
    pagination_class = None
    serializer_class = MeInstituteSerializer

    def get(self, request):
        student = _student(request)
        return Response(MeInstituteSerializer.from_institute(student.institute))


class NotificationListView(StudentAPIMixin, ListAPIView):
    serializer_class = NotificationSerializer

    def get_queryset(self):
        return Notification.objects.filter(
            student=_student(self.request),
            is_active=True,
        ).order_by("-created_at")


class NotificationReadView(StudentAPIMixin, APIView):
    serializer_class = NotificationSerializer

    def post(self, request, notification_id):
        notification = mark_notification_read(_student(request), notification_id)
        return Response(NotificationSerializer(notification).data)


class _NotificationReadAllSerializer(serializers.Serializer):
    updated = serializers.IntegerField()


class NotificationReadAllView(StudentAPIMixin, APIView):
    serializer_class = _NotificationReadAllSerializer

    def post(self, request):
        updated = mark_all_notifications_read(_student(request))
        return Response({"updated": updated})


class NotificationPreferencesView(StudentAPIMixin, APIView):
    pagination_class = None
    serializer_class = NotificationPreferenceSerializer

    def get(self, request):
        prefs = get_or_create_preferences(_student(request))
        return Response(NotificationPreferenceSerializer(prefs).data)

    def put(self, request):
        prefs = get_or_create_preferences(_student(request))
        serializer = NotificationPreferenceSerializer(
            prefs, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(NotificationPreferenceSerializer(prefs).data)
