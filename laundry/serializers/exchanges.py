"""Student exchange serializers (camelCase for the mobile app)."""

from __future__ import annotations

from rest_framework import serializers

from laundry.models import Exchange, ExchangeKind, Student
from laundry.serializers.student import BookingSerializer


def _initials(name: str) -> str:
    parts = [part for part in (name or "").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


class ExchangePartySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    initials = serializers.CharField()

    @classmethod
    def from_student(cls, student: Student) -> dict:
        return {
            "id": student.id,
            "name": student.name,
            "initials": _initials(student.name),
        }


class ExchangeCreateSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=ExchangeKind.choices)
    targetBookingId = serializers.UUIDField(required=False)
    target_booking_id = serializers.UUIDField(required=False)
    offeredBookingId = serializers.UUIDField(required=False, allow_null=True)
    offered_booking_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        target = attrs.get("targetBookingId") or attrs.get("target_booking_id")
        if target is None:
            raise serializers.ValidationError("Provide targetBookingId.")
        offered = attrs.get("offeredBookingId")
        if offered is None:
            offered = attrs.get("offered_booking_id")
        return {
            "kind": attrs["kind"],
            "target_booking_id": target,
            "offered_booking_id": offered,
        }


class ExchangeRejectSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ExchangeSerializer(serializers.ModelSerializer):
    requester = serializers.SerializerMethodField()
    holder = serializers.SerializerMethodField()
    targetBooking = BookingSerializer(source="target_booking", read_only=True)
    offeredBooking = BookingSerializer(
        source="offered_booking", read_only=True, allow_null=True
    )
    failureReason = serializers.CharField(source="failure_reason", read_only=True)
    rejectNote = serializers.CharField(source="reject_note", read_only=True)
    resolvedAt = serializers.DateTimeField(source="resolved_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    direction = serializers.SerializerMethodField()

    class Meta:
        model = Exchange
        fields = (
            "id",
            "kind",
            "status",
            "requester",
            "holder",
            "targetBooking",
            "offeredBooking",
            "failureReason",
            "rejectNote",
            "resolvedAt",
            "createdAt",
            "direction",
        )

    def get_requester(self, obj: Exchange) -> dict:
        return ExchangePartySerializer.from_student(obj.requester)

    def get_holder(self, obj: Exchange) -> dict:
        return ExchangePartySerializer.from_student(obj.holder)

    def get_direction(self, obj: Exchange) -> str | None:
        student = self.context.get("student")
        if student is None:
            return None
        if obj.holder_id == student.pk:
            return "incoming"
        if obj.requester_id == student.pk:
            return "outgoing"
        return None
