"""Serializers for the audit log, dashboard, and impact previews."""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from laundry.models import AdminAuditLog


class AuditEntrySerializer(serializers.ModelSerializer):
    actor_id = serializers.UUIDField(source="actor.id", read_only=True, allow_null=True)
    action_label = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = AdminAuditLog
        fields = (
            "id",
            "created_at",
            "actor_id",
            "actor_label",
            "action",
            "action_label",
            "target_type",
            "target_id",
            "target_label",
            "summary",
            "metadata",
        )
        read_only_fields = fields


class AffectedBookingSerializer(serializers.Serializer):
    """One booking an admin action is about to disturb."""

    id = serializers.UUIDField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    student_id = serializers.UUIDField()
    student_name = serializers.CharField()
    student_email = serializers.EmailField()


class ImpactPreviewSerializer(serializers.Serializer):
    """
    What an action would do, before it is done.

    The portal must show "how many upcoming bookings will be cancelled and which
    students will be notified" *before* the administrator commits.
    """

    affected_count = serializers.IntegerField()
    students_notified = serializers.IntegerField()
    bookings = AffectedBookingSerializer(many=True)


class DashboardSummarySerializer(serializers.Serializer):
    date = serializers.DateField()
    bookings = serializers.IntegerField()
    capacity_slots = serializers.IntegerField()
    capacity_used_pct = serializers.IntegerField()
    open_tickets = serializers.IntegerField()
    machines_total = serializers.IntegerField()
    machines_offline = serializers.IntegerField()
    students_total = serializers.IntegerField()
    suspended_students = serializers.IntegerField()


class AttentionItemSerializer(serializers.Serializer):
    kind = serializers.CharField()
    severity = serializers.CharField()
    title = serializers.CharField()
    detail = serializers.CharField()
    target_type = serializers.CharField()
    target_id = serializers.UUIDField(allow_null=True)


class ActivityItemSerializer(serializers.Serializer):
    at = serializers.DateTimeField()
    kind = serializers.CharField()
    summary = serializers.CharField()
    actor = serializers.CharField(allow_blank=True)
    target_type = serializers.CharField(allow_blank=True)
    target_id = serializers.UUIDField(allow_null=True)


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


class MachineHoursImpactRequestSerializer(serializers.Serializer):
    operating_window_start = serializers.TimeField()
    operating_window_end = serializers.TimeField()
