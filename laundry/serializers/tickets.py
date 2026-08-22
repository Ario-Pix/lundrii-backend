"""Student-facing ticket serializers (camelCase for the mobile app)."""

from __future__ import annotations

from rest_framework import serializers

from laundry.models import Ticket, TicketKind


class StudentTicketSerializer(serializers.ModelSerializer):
    """List/detail shape: status, note, photoUrl, machine, timestamps — no thread."""

    kind = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    title = serializers.SerializerMethodField()
    note = serializers.CharField(source="student_note", read_only=True)
    studentNote = serializers.CharField(source="student_note", read_only=True)
    photoUrl = serializers.URLField(source="photo_url", read_only=True, allow_blank=True)
    committeeNote = serializers.SerializerMethodField()
    machineId = serializers.UUIDField(source="machine_id", read_only=True)
    machineName = serializers.CharField(source="machine.location_name", read_only=True)
    hostelId = serializers.UUIDField(source="machine.hostel_id", read_only=True)
    hostelName = serializers.CharField(source="machine.hostel.name", read_only=True)
    bookingId = serializers.UUIDField(source="booking_id", read_only=True, allow_null=True)
    slotStart = serializers.DateTimeField(source="slot_start", read_only=True, allow_null=True)
    recordedHolder = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)
    resolvedAt = serializers.DateTimeField(source="resolved_at", read_only=True, allow_null=True)

    class Meta:
        model = Ticket
        fields = (
            "id",
            "number",
            "kind",
            "status",
            "title",
            "note",
            "studentNote",
            "photoUrl",
            "committeeNote",
            "machineId",
            "machineName",
            "hostelId",
            "hostelName",
            "bookingId",
            "slotStart",
            "recordedHolder",
            "createdAt",
            "updatedAt",
            "resolvedAt",
        )
        read_only_fields = fields

    def get_title(self, obj: Ticket) -> str:
        return "Machine not working"

    def get_committeeNote(self, obj: Ticket) -> str | None:
        note = (obj.committee_note or "").strip()
        return note or None

    def get_recordedHolder(self, obj: Ticket) -> dict | None:
        holder = obj.recorded_holder
        if holder is None:
            return None
        return {"id": holder.id, "name": holder.name}


class StudentTicketCreateSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(
        choices=[(TicketKind.MAINTENANCE, "Maintenance")],
        default=TicketKind.MAINTENANCE,
        required=False,
    )
    note = serializers.CharField(required=True, allow_blank=False, trim_whitespace=True)
    machineId = serializers.UUIDField(required=True)
    photo = serializers.FileField(required=False, allow_null=True)
