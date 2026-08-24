"""Student-facing machine / booking serializers (camelCase for the mobile app)."""

from __future__ import annotations

from django.utils import timezone as dj_tz
from rest_framework import serializers

from laundry.models import (
    AvailabilityMiss,
    Booking,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    Notification,
    NotificationPreference,
    Strike,
    Student,
)
from laundry.services.notifications import notification_deep_link
from laundry.services.rules import (
    cooldown_clears_at,
    get_institute_rules,
    quota_status,
    student_gender,
)
from laundry.services.slots import DerivedSlot


class InstituteDateTimeField(serializers.DateTimeField):
    """Naive `startsAt` is the institute wall clock (Asia/Kolkata), not UTC."""

    def __init__(self, **kwargs):
        kwargs.setdefault("default_timezone", dj_tz.get_current_timezone())
        super().__init__(**kwargs)


class HolderSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class MachineCardSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    kind = serializers.CharField()
    status = serializers.CharField()
    hostelId = serializers.UUIDField()
    hostelName = serializers.CharField()
    subtitle = serializers.CharField()
    isOffline = serializers.BooleanField()
    slotLengthMinutes = serializers.IntegerField()
    operatingWindowStart = serializers.TimeField()
    operatingWindowEnd = serializers.TimeField()
    openSlotsToday = serializers.IntegerField()
    freeUntil = serializers.DateTimeField(allow_null=True)
    freesAt = serializers.DateTimeField(allow_null=True)
    runningUntil = serializers.DateTimeField(allow_null=True)
    nextSlotStartsAt = serializers.DateTimeField(allow_null=True)

    @classmethod
    def from_machine(cls, machine: Machine, live: dict) -> dict:
        hostel = machine.hostel
        return {
            "id": machine.id,
            "name": machine.location_name,
            "kind": machine.kind,
            "status": live["status"],
            "hostelId": hostel.id,
            "hostelName": hostel.name,
            "subtitle": live["subtitle"],
            "isOffline": machine.is_offline,
            "slotLengthMinutes": machine.slot_length_minutes,
            "operatingWindowStart": machine.operating_window_start,
            "operatingWindowEnd": machine.operating_window_end,
            "openSlotsToday": live["open_slots_today"],
            "freeUntil": live.get("free_until"),
            "freesAt": live.get("frees_at"),
            "runningUntil": live.get("running_until"),
            "nextSlotStartsAt": live.get("next_slot_starts_at"),
        }


class SlotSerializer(serializers.Serializer):
    startsAt = serializers.DateTimeField()
    endsAt = serializers.DateTimeField()
    hour = serializers.IntegerField()
    state = serializers.CharField()
    label = serializers.CharField(allow_null=True)
    isMine = serializers.BooleanField()
    holder = HolderSerializer(allow_null=True)
    bookingId = serializers.UUIDField(allow_null=True)
    blockedRule = serializers.CharField(allow_null=True)
    clearsAt = serializers.DateTimeField(allow_null=True)

    @classmethod
    def from_slot(cls, slot: DerivedSlot) -> dict:
        holder = None
        if slot.holder_id and not slot.is_mine:
            holder = {"id": slot.holder_id, "name": slot.holder_name or ""}
        elif slot.holder_id and slot.state == "running":
            holder = {"id": slot.holder_id, "name": slot.holder_name or ""}
        return {
            "startsAt": slot.starts_at,
            "endsAt": slot.ends_at,
            "hour": slot.hour,
            "state": slot.state,
            "label": slot.label,
            "isMine": slot.is_mine,
            "holder": holder,
            "bookingId": slot.booking_id,
            "blockedRule": slot.blocked_rule,
            "clearsAt": slot.clears_at,
        }


class BookingSerializer(serializers.ModelSerializer):
    machineId = serializers.UUIDField(source="machine_id", read_only=True)
    machineName = serializers.CharField(source="machine.location_name", read_only=True)
    kind = serializers.CharField(source="machine.kind", read_only=True)
    hostelId = serializers.UUIDField(source="machine.hostel_id", read_only=True)
    hostelName = serializers.CharField(source="machine.hostel.name", read_only=True)
    startsAt = serializers.DateTimeField(source="starts_at", read_only=True)
    endsAt = serializers.DateTimeField(source="ends_at", read_only=True)
    hour = serializers.SerializerMethodField()
    isLateCancel = serializers.BooleanField(source="is_late_cancel", read_only=True)
    countsAgainstQuota = serializers.BooleanField(
        source="counts_against_quota", read_only=True
    )
    cancelledAt = serializers.DateTimeField(source="cancelled_at", read_only=True)
    isCancelled = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = (
            "id",
            "machineId",
            "machineName",
            "kind",
            "hostelId",
            "hostelName",
            "startsAt",
            "endsAt",
            "hour",
            "isLateCancel",
            "countsAgainstQuota",
            "cancelledAt",
            "isCancelled",
        )

    def get_hour(self, obj: Booking) -> int:
        return dj_tz.localtime(obj.starts_at).hour

    def get_isCancelled(self, obj: Booking) -> bool:
        return obj.cancelled_at is not None


class BookingCreateItemSerializer(serializers.Serializer):
    machineId = serializers.UUIDField()
    startsAt = InstituteDateTimeField(required=False, allow_null=True)
    date = serializers.DateField(required=False, allow_null=True)
    hour = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=23)

    def validate(self, attrs):
        if attrs.get("startsAt") is None and (
            attrs.get("date") is None or attrs.get("hour") is None
        ):
            raise serializers.ValidationError("Provide startsAt or date and hour.")
        return attrs


class BookingCreateSerializer(serializers.Serializer):
    items = BookingCreateItemSerializer(many=True, required=False)
    machineId = serializers.UUIDField(required=False)
    startsAt = InstituteDateTimeField(required=False, allow_null=True)
    date = serializers.DateField(required=False, allow_null=True)
    hour = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=23)

    def validate(self, attrs):
        items = attrs.get("items")
        if items:
            return {"items": items}
        if attrs.get("machineId") is None:
            raise serializers.ValidationError("Provide items or machineId.")
        item = {"machineId": attrs["machineId"]}
        if attrs.get("startsAt") is not None:
            item["startsAt"] = attrs["startsAt"]
        if attrs.get("date") is not None:
            item["date"] = attrs["date"]
        if attrs.get("hour") is not None:
            item["hour"] = attrs["hour"]
        nested = BookingCreateItemSerializer(data=item)
        nested.is_valid(raise_exception=True)
        return {"items": [nested.validated_data]}


class BookingMoveSerializer(serializers.Serializer):
    machineId = serializers.UUIDField(required=False)
    startsAt = InstituteDateTimeField(required=False, allow_null=True)
    date = serializers.DateField(required=False, allow_null=True)
    hour = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=23)

    def validate(self, attrs):
        if attrs.get("startsAt") is None and (
            attrs.get("date") is None or attrs.get("hour") is None
        ):
            raise serializers.ValidationError("Provide startsAt or date and hour.")
        return attrs


class AvailabilityMissCreateSerializer(serializers.Serializer):
    machineId = serializers.UUIDField()
    date = serializers.DateField()
    hour = serializers.IntegerField(min_value=0, max_value=23)


class AvailabilityMissSerializer(serializers.ModelSerializer):
    machineId = serializers.UUIDField(source="machine_id", read_only=True)

    class Meta:
        model = AvailabilityMiss
        fields = ("id", "machineId", "date", "hour", "created_at")


class MoveOptionSerializer(serializers.Serializer):
    machineId = serializers.UUIDField()
    machineName = serializers.CharField()
    hostelId = serializers.UUIDField()
    hostelName = serializers.CharField()
    startsAt = serializers.DateTimeField()
    endsAt = serializers.DateTimeField()
    hour = serializers.IntegerField()

    @classmethod
    def from_option(cls, row: dict) -> dict:
        machine: Machine = row["machine"]
        return {
            "machineId": machine.id,
            "machineName": machine.location_name,
            "hostelId": machine.hostel_id,
            "hostelName": machine.hostel.name,
            "startsAt": row["starts_at"],
            "endsAt": row["ends_at"],
            "hour": row["hour"],
        }


class StrikeSerializer(serializers.ModelSerializer):
    ticketNumber = serializers.SerializerMethodField()

    class Meta:
        model = Strike
        fields = ("id", "reason", "date", "ticketNumber")

    def get_ticketNumber(self, obj: Strike) -> int | None:
        ticket = obj.ticket
        if ticket is None or ticket.number is None:
            return None
        return ticket.number


class QuotaSerializer(serializers.Serializer):
    used = serializers.IntegerField()
    limit = serializers.IntegerField()
    dryerUsed = serializers.IntegerField()
    windowDays = serializers.IntegerField()
    resetsAt = serializers.DateTimeField(allow_null=True)


class MeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    email = serializers.EmailField()
    phone = serializers.CharField()
    whatsappOptIn = serializers.BooleanField()
    hostelId = serializers.UUIDField(allow_null=True)
    hostelName = serializers.CharField(allow_null=True)
    floor = serializers.CharField(allow_null=True, allow_blank=True)
    gender = serializers.CharField(allow_null=True)
    emailVerified = serializers.BooleanField()
    suspended = serializers.BooleanField()
    suspensionEnds = serializers.DateTimeField(allow_null=True)
    suspensionReason = serializers.CharField(allow_null=True)
    quota = QuotaSerializer()
    cooldownClearsAt = serializers.DateTimeField(allow_null=True)
    strikes = StrikeSerializer(many=True)

    @classmethod
    def from_student(cls, student: Student, *, now=None) -> dict:
        hostel = student.home_hostel
        quota = quota_status(student, now=now)
        reason = (student.suspension_reason or "").strip() or None
        strikes = student.strikes.filter(is_active=True).select_related("ticket")
        return {
            "id": student.id,
            "name": student.name,
            "email": student.user.email,
            "phone": student.phone,
            "whatsappOptIn": student.whatsapp_opt_in,
            "hostelId": hostel.id if hostel else None,
            "hostelName": hostel.name if hostel else None,
            "floor": student.floor or None,
            "gender": student_gender(student) or None,
            "emailVerified": student.is_email_verified,
            "suspended": student.is_suspended,
            "suspensionEnds": student.suspension_ends,
            "suspensionReason": reason,
            "quota": {
                "used": quota["used"],
                "limit": quota["limit"],
                "dryerUsed": quota["dryer_used"],
                "windowDays": quota["window_days"],
                "resetsAt": quota["resets_at"],
            },
            "cooldownClearsAt": cooldown_clears_at(student, now=now),
            "strikes": StrikeSerializer(strikes, many=True).data,
        }


class MeUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    phone = serializers.CharField(max_length=32, required=False)
    whatsappOptIn = serializers.BooleanField(required=False)
    whatsapp_opt_in = serializers.BooleanField(required=False)
    hostelId = serializers.UUIDField(required=False)
    hostel_id = serializers.UUIDField(required=False)

    def validate_name(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value

    def validate_phone(self, value: str) -> str:
        return (value or "").strip()

    def validate(self, attrs):
        if "whatsappOptIn" in attrs and "whatsapp_opt_in" not in attrs:
            attrs["whatsapp_opt_in"] = attrs.pop("whatsappOptIn")
        else:
            attrs.pop("whatsappOptIn", None)

        student: Student = self.instance
        hostel_id = attrs.pop("hostelId", None)
        if hostel_id is None:
            hostel_id = attrs.pop("hostel_id", None)
        else:
            attrs.pop("hostel_id", None)

        new_hostel: Hostel | None = None
        if hostel_id is not None:
            try:
                new_hostel = Hostel.objects.select_related("institute").get(
                    pk=hostel_id, is_active=True
                )
            except (Hostel.DoesNotExist, ValueError, TypeError) as exc:
                raise serializers.ValidationError(
                    {"hostelId": ["Unknown hostel."]}
                ) from exc
            if new_hostel.institute_id != student.institute_id:
                raise serializers.ValidationError(
                    {"hostelId": ["Hostel is outside your institute."]}
                )
            attrs["home_hostel"] = new_hostel

        return attrs

    def update(self, instance: Student, validated_data) -> Student:
        if "home_hostel" in validated_data:
            instance.home_hostel = validated_data.pop("home_hostel")
        if "name" in validated_data:
            instance.name = validated_data["name"]
        if "phone" in validated_data:
            instance.phone = validated_data["phone"]
        if "whatsapp_opt_in" in validated_data:
            instance.whatsapp_opt_in = validated_data["whatsapp_opt_in"]
        instance.save()
        return instance


class EligibleHostelSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    isHome = serializers.BooleanField()

    @classmethod
    def from_hostel(cls, hostel: Hostel, student: Student) -> dict:
        return {
            "id": hostel.id,
            "name": hostel.name,
            "isHome": bool(
                student.home_hostel_id and hostel.id == student.home_hostel_id
            ),
        }


class InstituteRulesPublicSerializer(serializers.Serializer):
    quotaLimit = serializers.IntegerField()
    quotaWindowDays = serializers.IntegerField()
    cooldownHours = serializers.IntegerField()
    advanceWindowDays = serializers.IntegerField()
    cancellationCutoffHours = serializers.IntegerField()
    dryerCapEnabled = serializers.BooleanField()

    @classmethod
    def from_rules(cls, rules: InstituteRule) -> dict:
        return {
            "quotaLimit": rules.quota_limit,
            "quotaWindowDays": rules.quota_window_days,
            "cooldownHours": rules.cooldown_hours,
            "advanceWindowDays": rules.advance_window_days,
            "cancellationCutoffHours": rules.cancellation_cutoff_hours,
            "dryerCapEnabled": rules.dryer_cap_enabled,
        }


class MeInstituteSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    allowedDomains = serializers.ListField(child=serializers.CharField())
    rules = InstituteRulesPublicSerializer()

    @classmethod
    def from_institute(cls, institute: Institute) -> dict:
        rules = get_institute_rules(institute)
        return {
            "id": institute.id,
            "name": institute.name,
            "allowedDomains": list(institute.allowed_email_domains or []),
            "rules": InstituteRulesPublicSerializer.from_rules(rules),
        }


class NotificationSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    read = serializers.SerializerMethodField()
    deepLink = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = (
            "id",
            "title",
            "body",
            "kind",
            "type",
            "createdAt",
            "read",
            "deepLink",
        )

    def get_read(self, obj: Notification) -> bool:
        return obj.read_at is not None

    def get_deepLink(self, obj: Notification) -> str | None:
        return notification_deep_link(obj)


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    bookingConfirmed = serializers.BooleanField(source="booking_confirmed", required=False)
    slotReminder = serializers.BooleanField(source="slot_reminder", required=False)
    bookingCancelledOffline = serializers.BooleanField(
        source="booking_cancelled_offline", required=False
    )
    exchangeRequest = serializers.BooleanField(source="exchange_request", required=False)
    exchangeOutcome = serializers.BooleanField(source="exchange_outcome", required=False)
    ticketUpdate = serializers.BooleanField(source="ticket_update", required=False)
    strike = serializers.BooleanField(required=False)
    suspension = serializers.BooleanField(required=False)

    class Meta:
        model = NotificationPreference
        fields = (
            "bookingConfirmed",
            "slotReminder",
            "bookingCancelledOffline",
            "exchangeRequest",
            "exchangeOutcome",
            "ticketUpdate",
            "strike",
            "suspension",
        )

