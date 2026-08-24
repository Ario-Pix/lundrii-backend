"""Serializers for administrator / super-administrator CRUD APIs."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from authentication.services.institutes import domains_of, email_domain
from laundry.models import (
    Booking,
    Gender,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    Strike,
    Student,
    Ticket,
    TicketEvent,
    TicketStatus,
)
from laundry.permissions import scoped_institute_id

User = get_user_model()


def _clean_allowed_email_domains(value):
    if not isinstance(value, list):
        raise serializers.ValidationError("Must be a list of domain strings.")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise serializers.ValidationError("Each domain must be a non-empty string.")
        domain = item.strip().lower().lstrip("@")
        if " " in domain or "/" in domain or "." not in domain:
            raise serializers.ValidationError(f"Invalid domain: {item}")
        cleaned.append(domain)
    return cleaned


class InstituteRuleSerializer(serializers.ModelSerializer):
    institute = serializers.PrimaryKeyRelatedField(
        queryset=Institute.objects.all(), required=False
    )
    allowed_email_domains = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        write_only=True,
        help_text="Optional write-through to the parent institute allow-list.",
    )

    class Meta:
        model = InstituteRule
        fields = (
            "id",
            "institute",
            "quota_limit",
            "quota_window_days",
            "cooldown_hours",
            "advance_window_days",
            "cancellation_cutoff_hours",
            "dryer_cap_enabled",
            "allowed_email_domains",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def to_internal_value(self, data):
        scoped = scoped_institute_id(self.context["request"].user)
        if scoped is not None and not (hasattr(data, "get") and data.get("institute")):
            data = data.copy()
            data["institute"] = str(scoped)
        return super().to_internal_value(data)

    def validate_cooldown_hours(self, value):
        return 0

    def validate_allowed_email_domains(self, value):
        return _clean_allowed_email_domains(value)

    def validate(self, attrs):
        request = self.context["request"]
        scoped = scoped_institute_id(request.user)

        if self.instance is not None:
            attrs.pop("institute", None)
            institute = self.instance.institute
        elif scoped is not None:
            institute = Institute.objects.get(pk=scoped)
            attrs["institute"] = institute
        else:
            institute = attrs.get("institute")

        if institute is None:
            raise serializers.ValidationError({"institute": "This field is required."})

        qs = InstituteRule.objects.filter(institute=institute)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                {"institute": "Rules already exist for this institute."}
            )
        return attrs

    def create(self, validated_data):
        domains = validated_data.pop("allowed_email_domains", None)
        rule = super().create(validated_data)
        if domains is not None:
            institute = rule.institute
            institute.allowed_email_domains = domains
            institute.save(update_fields=["allowed_email_domains", "updated_at"])
        return rule

    def update(self, instance, validated_data):
        domains = validated_data.pop("allowed_email_domains", None)
        rule = super().update(instance, validated_data)
        if domains is not None:
            institute = rule.institute
            institute.allowed_email_domains = domains
            institute.save(update_fields=["allowed_email_domains", "updated_at"])
        return rule


class InstituteSerializer(serializers.ModelSerializer):
    rules = InstituteRuleSerializer(read_only=True)

    class Meta:
        model = Institute
        fields = (
            "id",
            "name",
            "allowed_email_domains",
            "is_active",
            "rules",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_allowed_email_domains(self, value):
        return _clean_allowed_email_domains(value)

    def validate(self, attrs):
        request = self.context.get("request")
        if request is None:
            return attrs
        scoped = scoped_institute_id(request.user)
        # Institute admins may only change the allow-list (not name / is_active).
        if scoped is not None and self.instance is not None:
            forbidden = set(attrs) - {"allowed_email_domains"}
            if forbidden:
                raise serializers.ValidationError(
                    {field: "Only allowed_email_domains may be updated." for field in forbidden}
                )
        return attrs


class HostelSerializer(serializers.ModelSerializer):
    institute = serializers.PrimaryKeyRelatedField(
        queryset=Institute.objects.all(), required=False
    )
    institute_name = serializers.CharField(source="institute.name", read_only=True)
    machine_count = serializers.IntegerField(read_only=True)
    resident_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Hostel
        fields = (
            "id",
            "institute",
            "institute_name",
            "name",
            "machine_count",
            "resident_count",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def to_internal_value(self, data):
        # UniqueConstraint(institute, name) makes DRF require institute in input.
        scoped = scoped_institute_id(self.context["request"].user)
        if scoped is not None and not (hasattr(data, "get") and data.get("institute")):
            data = data.copy()
            data["institute"] = str(scoped)
        return super().to_internal_value(data)

    def validate_institute(self, institute):
        scoped = scoped_institute_id(self.context["request"].user)
        if scoped is not None and institute.id != scoped:
            raise serializers.ValidationError("Cannot manage hostels outside your institute.")
        return institute

    def validate(self, attrs):
        request = self.context["request"]
        scoped = scoped_institute_id(request.user)
        if self.instance is None and scoped is None and not attrs.get("institute"):
            raise serializers.ValidationError({"institute": "This field is required."})
        return attrs


class MachineSerializer(serializers.ModelSerializer):
    hostel_name = serializers.CharField(source="hostel.name", read_only=True)
    institute = serializers.UUIDField(source="hostel.institute_id", read_only=True)

    class Meta:
        model = Machine
        fields = (
            "id",
            "hostel",
            "hostel_name",
            "institute",
            "kind",
            "location_name",
            "operating_window_start",
            "operating_window_end",
            "slot_length_minutes",
            "is_offline",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_hostel(self, hostel):
        scoped = scoped_institute_id(self.context["request"].user)
        if scoped is not None and hostel.institute_id != scoped:
            raise serializers.ValidationError("Hostel is outside your institute.")
        return hostel

    def validate_slot_length_minutes(self, value):
        if value < 1:
            raise serializers.ValidationError("Slot length must be at least 1 minute.")
        return value


class MachineOfflineSerializer(serializers.Serializer):
    is_offline = serializers.BooleanField()


class MachineHoursSerializer(serializers.Serializer):
    operating_window_start = serializers.TimeField()
    operating_window_end = serializers.TimeField()
    slot_length_minutes = serializers.IntegerField(required=False, min_value=1)

    def save(self, **kwargs):
        machine = self.instance
        machine.operating_window_start = self.validated_data["operating_window_start"]
        machine.operating_window_end = self.validated_data["operating_window_end"]
        update_fields = [
            "operating_window_start",
            "operating_window_end",
            "updated_at",
        ]
        if "slot_length_minutes" in self.validated_data:
            machine.slot_length_minutes = self.validated_data["slot_length_minutes"]
            update_fields.append("slot_length_minutes")
        machine.save(update_fields=update_fields)
        return machine


class AdminProfileSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    display_name = serializers.CharField(max_length=150)
    email = serializers.EmailField(read_only=True)
    institute_id = serializers.UUIDField(allow_null=True, read_only=True)
    institute_name = serializers.CharField(allow_null=True, read_only=True)
    role_label = serializers.CharField(read_only=True)
    initials = serializers.CharField(read_only=True)


# The student API exposes its own, much narrower Strike representation
# (laundry.serializers.student.StrikeSerializer). Both classes are named
# StrikeSerializer, so without an explicit component name they would collide on
# "Strike" in the schema and one would silently overwrite the other. The name
# follows the Admin* convention already used by AdminStudent / AdminTicket.
@extend_schema_serializer(component_name="AdminStrike")
class StrikeSerializer(serializers.ModelSerializer):
    recorded_by_email = serializers.EmailField(source="recorded_by.email", read_only=True)
    date = serializers.DateField(required=False)

    class Meta:
        model = Strike
        fields = (
            "id",
            "student",
            "reason",
            "date",
            "recorded_by",
            "recorded_by_email",
            "ticket",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "student",
            "recorded_by",
            "is_active",
            "created_at",
            "updated_at",
        )

    def validate_ticket(self, ticket):
        student = self.context.get("student")
        if ticket is not None and student is not None and ticket.student_id != student.id:
            raise serializers.ValidationError("Ticket does not belong to this student.")
        return ticket

    def validate_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Strike date cannot be in the future.")
        return value

    def validate(self, attrs):
        attrs.setdefault("date", timezone.localdate())
        return attrs


class AdminStudentSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    is_suspended = serializers.BooleanField(read_only=True)
    is_email_verified = serializers.BooleanField(read_only=True)
    home_hostel_name = serializers.SerializerMethodField()
    strike_count = serializers.SerializerMethodField()
    institute_name = serializers.CharField(source="institute.name", read_only=True)

    class Meta:
        model = Student
        fields = (
            "id",
            "email",
            "name",
            "phone",
            "whatsapp_opt_in",
            "gender",
            "home_hostel",
            "home_hostel_name",
            "institute",
            "institute_name",
            "email_verified_at",
            "is_email_verified",
            "suspension_ends",
            "suspension_reason",
            "is_suspended",
            "is_active",
            "strike_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_home_hostel_name(self, obj) -> str | None:
        return obj.home_hostel.name if obj.home_hostel_id else None

    def get_strike_count(self, obj) -> int:
        annotated = getattr(obj, "strike_count", None)
        if annotated is not None:
            return annotated
        return obj.strikes.filter(is_active=True).count()


class StudentCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    gender = serializers.ChoiceField(choices=Gender.choices)
    home_hostel = serializers.PrimaryKeyRelatedField(
        queryset=Hostel.objects.all(), required=False
    )
    hostel = serializers.PrimaryKeyRelatedField(
        queryset=Hostel.objects.all(), required=False
    )

    def validate_email(self, value):
        email = value.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return email

    def validate(self, attrs):
        request = self.context["request"]
        hostel = attrs.get("home_hostel") or attrs.get("hostel")
        if hostel is None:
            raise serializers.ValidationError(
                {"home_hostel": "This field is required (alias: hostel)."}
            )
        attrs["home_hostel"] = hostel
        attrs.pop("hostel", None)

        scoped = scoped_institute_id(request.user)
        institute = hostel.institute
        if scoped is not None and hostel.institute_id != scoped:
            raise serializers.ValidationError(
                {"home_hostel": "Hostel is outside your institute."}
            )

        domain = email_domain(attrs["email"])
        if domain not in domains_of(institute):
            raise serializers.ValidationError(
                {"email": f"Email domain '{domain}' is not allowed for this institute."}
            )
        attrs["institute"] = institute
        return attrs

    def create(self, validated_data):
        institute = validated_data["institute"]
        with transaction.atomic():
            user = User.objects.create_user(
                email=validated_data["email"],
                password=None,
            )
            user.set_unusable_password()
            user.save(update_fields=["password", "updated_at"])
            student = Student.objects.create(
                user=user,
                institute=institute,
                name=validated_data["name"],
                phone=validated_data.get("phone") or "",
                gender=validated_data["gender"],
                home_hostel=validated_data["home_hostel"],
            )
        return student


class StudentBookingHistoryItemSerializer(serializers.Serializer):
    when = serializers.CharField()
    machine = serializers.CharField()
    hostel = serializers.CharField()
    status = serializers.CharField()


class SuspensionRowSerializer(serializers.Serializer):
    student_id = serializers.UUIDField()
    student_name = serializers.CharField()
    student_email = serializers.EmailField()
    until = serializers.CharField()
    reason = serializers.CharField()
    by = serializers.CharField()


def booking_history_status(booking: Booking, *, now=None) -> str:
    now = now or timezone.now()
    if booking.cancelled_at is not None:
        return "Cancelled"
    if booking.starts_at > now:
        return "Upcoming"
    return "Completed"


def serialize_booking_history(booking: Booking, *, now=None) -> dict:
    when = timezone.localtime(booking.starts_at).strftime("%Y-%m-%d %H:%M")
    return {
        "when": when,
        "machine": booking.machine.location_name,
        "hostel": booking.machine.hostel.name,
        "status": booking_history_status(booking, now=now),
    }


class StudentAssignSerializer(serializers.Serializer):
    gender = serializers.ChoiceField(choices=Gender.choices)
    home_hostel = serializers.PrimaryKeyRelatedField(queryset=Hostel.objects.all())

    def validate_home_hostel(self, hostel):
        student = self.instance
        if hostel.institute_id != student.institute_id:
            raise serializers.ValidationError("Hostel must belong to the student's institute.")
        scoped = scoped_institute_id(self.context["request"].user)
        if scoped is not None and hostel.institute_id != scoped:
            raise serializers.ValidationError("Hostel is outside your institute.")
        return hostel

    def save(self, **kwargs):
        student = self.instance
        student.gender = self.validated_data["gender"]
        student.home_hostel = self.validated_data["home_hostel"]
        student.save(update_fields=["gender", "home_hostel", "updated_at"])
        return student


class StudentSuspendSerializer(serializers.Serializer):
    suspension_ends = serializers.DateTimeField()
    suspension_reason = serializers.CharField(allow_blank=True, required=False, default="")

    def validate_suspension_ends(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("suspension_ends must be in the future.")
        return value

    def save(self, **kwargs):
        student = self.instance
        student.suspension_ends = self.validated_data["suspension_ends"]
        student.suspension_reason = self.validated_data.get("suspension_reason", "")
        student.save(update_fields=["suspension_ends", "suspension_reason", "updated_at"])
        return student


class TicketEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, default=None)

    class Meta:
        model = TicketEvent
        fields = ("id", "title", "note", "actor", "actor_email", "occurred_at")
        read_only_fields = fields


class AdminTicketSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.name", read_only=True)
    student_email = serializers.EmailField(source="student.user.email", read_only=True)
    machine_location = serializers.CharField(source="machine.location_name", read_only=True)
    hostel_name = serializers.CharField(source="machine.hostel.name", read_only=True)
    events = TicketEventSerializer(many=True, read_only=True)

    class Meta:
        model = Ticket
        fields = (
            "id",
            "number",
            "kind",
            "status",
            "student",
            "student_name",
            "student_email",
            "machine",
            "machine_location",
            "hostel_name",
            "booking",
            "recorded_holder",
            "slot_start",
            "student_note",
            "photo_url",
            "committee_note",
            "resolved_at",
            "events",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class AdminTicketUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = ("status", "committee_note")

    def validate_status(self, value):
        valid = {c[0] for c in TicketStatus.choices}
        if value not in valid:
            raise serializers.ValidationError("Invalid ticket status.")
        return value
