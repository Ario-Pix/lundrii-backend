"""Administrator / super-administrator CRUD APIs."""

import csv
import io

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from authentication.services.institutes import domains_of, email_domain
from authentication.services.links import create_reset_link
from authentication.services.otp import (
    OtpCooldown,
    OtpPurpose,
    OtpRateLimited,
    create_otp,
)
from base.tasks import send_password_reset_email_task
from laundry.services import admin_ops
from laundry.services.audit import Action as AuditAction
from laundry.services.audit import record as audit
from base.permissions import (
    IsAdministratorOrSuperAdministrator,
    IsSuperAdministrator,
    user_is_super_administrator,
)
from laundry.models import (
    Administrator,
    Booking,
    Gender,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    NotificationKind,
    NotificationType,
    Strike,
    Student,
    Ticket,
    TicketEvent,
    TicketStatus,
)
from laundry.permissions import scoped_institute_id
from laundry.serializers.admin import (
    AdminProfileSerializer,
    AdminStudentSerializer,
    AdminTicketSerializer,
    AdminTicketUpdateSerializer,
    HostelSerializer,
    InstituteRuleSerializer,
    InstituteSerializer,
    MachineHoursSerializer,
    MachineOfflineSerializer,
    MachineSerializer,
    SuspensionRowSerializer,
    StrikeSerializer,
    StudentAssignSerializer,
    StudentBookingHistoryItemSerializer,
    StudentCreateSerializer,
    StudentSuspendSerializer,
    serialize_booking_history,
)
from laundry.services.machines import (
    cancel_bookings_outside_hours,
    cancel_future_bookings_for_machine,
    set_machine_offline,
)
from laundry.services.notifications import create_in_app_notification


def _parse_bool(value):
    if value is None:
        return None
    lowered = str(value).strip().lower()
    if lowered in ("1", "true", "yes"):
        return True
    if lowered in ("0", "false", "no"):
        return False
    return None


def _param(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() != "all":
            return text
    return None


def _initials(display_name: str, email: str) -> str:
    parts = [p for p in (display_name or "").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    if parts:
        return parts[0][:2].upper()
    local = (email or "").split("@", 1)[0]
    return (local[:2] or "AD").upper()


def _admin_profile_payload(user) -> dict:
    if user_is_super_administrator(user):
        profile = user.superadministrator
        return {
            "id": profile.id,
            "display_name": profile.display_name,
            "email": user.email,
            "institute_id": None,
            "institute_name": None,
            "role_label": "Super administrator",
            "initials": _initials(profile.display_name, user.email),
        }
    profile = user.administrator
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "email": user.email,
        "institute_id": profile.institute_id,
        "institute_name": profile.institute.name,
        "role_label": "Administrator",
        "initials": _initials(profile.display_name, user.email),
    }


def _set_admin_display_name(user, display_name: str):
    if user_is_super_administrator(user):
        profile = user.superadministrator
        profile.display_name = display_name
        profile.save(update_fields=["display_name", "updated_at"])
        return
    profile = user.administrator
    profile.display_name = display_name
    profile.save(update_fields=["display_name", "updated_at"])


class InstituteScopedQuerysetMixin:
    """Limit querysets to the administrator's institute. Super admins see all."""

    institute_filter = "institute"

    def get_queryset(self):
        qs = super().get_queryset()
        if user_is_super_administrator(self.request.user):
            return qs
        institute_id = scoped_institute_id(self.request.user)
        if institute_id is None:
            return qs.none()
        return qs.filter(**{self.institute_filter: institute_id})


class SoftDestroyMixin:
    """DELETE sets ``is_active=False`` rather than removing the row."""

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])


class AdminProfileView(APIView):
    """GET/PATCH signed-in administrator profile (`me/` or `profile/`)."""

    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]
    serializer_class = AdminProfileSerializer

    def get(self, request):
        return Response(_admin_profile_payload(request.user))

    def patch(self, request):
        serializer = AdminProfileSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        display_name = serializer.validated_data.get("display_name")
        if display_name is not None:
            _set_admin_display_name(request.user, display_name)
        return Response(_admin_profile_payload(request.user))


class InstituteViewSet(SoftDestroyMixin, viewsets.ModelViewSet):
    """
    Super-admin: full institute CRUD (creates default rules on create).

    Institute admins: list/retrieve/PATCH their own institute (domains).
    """

    serializer_class = InstituteSerializer
    queryset = Institute.objects.all().select_related("rules")

    def get_permissions(self):
        if self.action in ("list", "retrieve", "partial_update", "update"):
            return [IsAuthenticated(), IsAdministratorOrSuperAdministrator()]
        return [IsAuthenticated(), IsSuperAdministrator()]

    def get_queryset(self):
        qs = super().get_queryset()
        if user_is_super_administrator(self.request.user):
            is_active = _parse_bool(self.request.query_params.get("is_active"))
            if is_active is not None:
                qs = qs.filter(is_active=is_active)
            return qs
        institute_id = scoped_institute_id(self.request.user)
        if institute_id is None:
            return qs.none()
        return qs.filter(pk=institute_id)

    def perform_create(self, serializer):
        institute = serializer.save()
        InstituteRule.objects.get_or_create(institute=institute)


class HostelViewSet(InstituteScopedQuerysetMixin, SoftDestroyMixin, viewsets.ModelViewSet):
    serializer_class = HostelSerializer
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]
    queryset = Hostel.objects.select_related("institute").all()
    institute_filter = "institute"

    def get_queryset(self):
        qs = super().get_queryset()
        is_active = _parse_bool(self.request.query_params.get("is_active"))
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def perform_create(self, serializer):
        institute_id = scoped_institute_id(self.request.user)
        if institute_id is not None:
            serializer.save(institute_id=institute_id)
        else:
            serializer.save()


class MachineViewSet(InstituteScopedQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = MachineSerializer
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]
    queryset = Machine.objects.select_related("hostel", "hostel__institute").all()
    institute_filter = "hostel__institute"

    def get_queryset(self):
        qs = super().get_queryset()
        is_active = _parse_bool(self.request.query_params.get("is_active"))
        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        status_alias = _param(self.request.query_params.get("status"))
        is_offline = _parse_bool(self.request.query_params.get("is_offline"))
        if status_alias == "offline":
            qs = qs.filter(is_offline=True)
        elif status_alias == "active":
            qs = qs.filter(is_offline=False, is_active=True)
        elif is_offline is not None:
            qs = qs.filter(is_offline=is_offline)

        hostel_id = _param(
            self.request.query_params.get("hostelId"),
            self.request.query_params.get("hostel"),
        )
        if hostel_id:
            qs = qs.filter(hostel_id=hostel_id)

        kind = _param(self.request.query_params.get("kind"))
        if kind:
            qs = qs.filter(kind=kind)
        return qs

    def perform_update(self, serializer):
        is_offline = serializer.validated_data.pop("is_offline", None)
        instance = serializer.save()
        if is_offline is not None:
            set_machine_offline(instance, is_offline)

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        if not instance.is_offline:
            set_machine_offline(instance, True)
        else:
            cancel_future_bookings_for_machine(instance)

    @action(detail=True, methods=["post"], url_path="offline")
    def offline(self, request, pk=None):
        machine = self.get_object()
        serializer = MachineOfflineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        going_offline = serializer.validated_data["is_offline"]
        # Snapshot before the write: afterwards the bookings are cancelled and
        # the affected students can no longer be listed.
        impact = admin_ops.offline_impact(machine) if going_offline else None
        cancelled = set_machine_offline(machine, going_offline)
        machine.refresh_from_db()
        audit(
            user=request.user,
            action=(
                AuditAction.MACHINE_OFFLINE if going_offline else AuditAction.MACHINE_ONLINE
            ),
            summary=(
                f"Took {machine.location_name} ({machine.hostel.name}) offline; "
                f"{cancelled} booking(s) cancelled."
                if going_offline
                else f"Brought {machine.location_name} ({machine.hostel.name}) back online."
            ),
            target=machine,
            target_type="machine",
            metadata={
                "cancelled_bookings": cancelled,
                "students_notified": impact["students_notified"] if impact else 0,
                "reason": serializer.validated_data.get("reason", ""),
            },
        )
        data = MachineSerializer(machine, context={"request": request}).data
        data["cancelled_bookings"] = cancelled
        return Response(data)

    @action(detail=True, methods=["post"], url_path="online")
    def online(self, request, pk=None):
        machine = self.get_object()
        set_machine_offline(machine, False)
        machine.refresh_from_db()
        audit(
            user=request.user,
            action=AuditAction.MACHINE_ONLINE,
            summary=f"Brought {machine.location_name} ({machine.hostel.name}) back online.",
            target=machine,
            target_type="machine",
        )
        return Response(MachineSerializer(machine, context={"request": request}).data)

    @action(detail=True, methods=["patch"], url_path="hours")
    def hours(self, request, pk=None):
        """
        Change the operating window.

        Narrowing it can strand bookings outside the new hours. Those are never
        cancelled silently — the caller must say `cancel_outside: true`, having
        seen what would go via `POST /admin/machines/{id}/hours-impact`.
        Stranded bookings are otherwise kept and reported back.
        """
        machine = self.get_object()
        serializer = MachineHoursSerializer(machine, data=request.data, partial=False)
        serializer.is_valid(raise_exception=True)

        impact = admin_ops.hours_change_impact(
            machine,
            serializer.validated_data["operating_window_start"],
            serializer.validated_data["operating_window_end"],
        )
        cancel_outside = str(request.data.get("cancel_outside", "")).lower() in (
            "1",
            "true",
            "yes",
        )

        serializer.save()
        machine.refresh_from_db()

        cancelled = 0
        if cancel_outside and impact["affected_count"]:
            cancelled = cancel_bookings_outside_hours(
                machine, [b["id"] for b in impact["bookings"]], actor=request.user
            )

        audit(
            user=request.user,
            action=AuditAction.MACHINE_HOURS,
            summary=(
                f"Changed hours for {machine.location_name} "
                f"({machine.hostel.name}) to "
                f"{machine.operating_window_start:%H:%M}–{machine.operating_window_end:%H:%M}."
            ),
            target=machine,
            target_type="machine",
            metadata={
                "stranded_bookings": impact["affected_count"],
                "cancelled_bookings": cancelled,
            },
        )

        data = MachineSerializer(machine, context={"request": request}).data
        data["stranded_bookings"] = impact["affected_count"]
        data["cancelled_bookings"] = cancelled
        return Response(data)


class InstituteRuleViewSet(
    InstituteScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = InstituteRuleSerializer
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]
    queryset = InstituteRule.objects.select_related("institute").all()
    institute_filter = "institute"

    def get_queryset(self):
        qs = super().get_queryset()
        institute_id = self.request.query_params.get("institute")
        if institute_id and user_is_super_administrator(self.request.user):
            qs = qs.filter(institute_id=institute_id)
        return qs

    def perform_create(self, serializer):
        institute_id = scoped_institute_id(self.request.user)
        if institute_id is not None:
            serializer.save(institute_id=institute_id)
        else:
            serializer.save()


class StudentViewSet(
    InstituteScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = AdminStudentSerializer
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]
    queryset = Student.objects.select_related("user", "institute", "home_hostel").all()
    institute_filter = "institute"
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_serializer_class(self):
        if self.action == "create":
            return StudentCreateSerializer
        return AdminStudentSerializer

    def get_queryset(self):
        qs = super().get_queryset().annotate(
            strike_count=Count("strikes", filter=Q(strikes__is_active=True))
        )
        is_active = _parse_bool(self.request.query_params.get("is_active"))
        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        gender = self.request.query_params.get("gender")
        if gender:
            qs = qs.filter(gender=gender)

        hostel_id = _param(
            self.request.query_params.get("hostelId"),
            self.request.query_params.get("home_hostel"),
            self.request.query_params.get("hostel"),
        )
        if hostel_id:
            qs = qs.filter(home_hostel_id=hostel_id)

        now = timezone.now()
        status_alias = _param(self.request.query_params.get("status"))
        suspended = _parse_bool(self.request.query_params.get("is_suspended"))
        if status_alias == "suspended":
            qs = qs.filter(suspension_ends__gt=now)
        elif status_alias == "disabled":
            qs = qs.filter(is_active=False)
        elif status_alias == "active":
            qs = qs.filter(is_active=True).filter(
                Q(suspension_ends__isnull=True) | Q(suspension_ends__lte=now)
            )
        elif suspended is True:
            qs = qs.filter(suspension_ends__gt=now)
        elif suspended is False:
            qs = qs.filter(Q(suspension_ends__isnull=True) | Q(suspension_ends__lte=now))

        strikes = _param(self.request.query_params.get("strikes"))
        if strikes == "with":
            qs = qs.filter(strike_count__gt=0)
        elif strikes == "none":
            qs = qs.filter(strike_count=0)

        search = _param(
            self.request.query_params.get("q"),
            self.request.query_params.get("query"),
            self.request.query_params.get("search"),
        )
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(user__email__icontains=search)
                | Q(phone__icontains=search)
            )
        return qs.order_by("name")

    def create(self, request, *args, **kwargs):
        serializer = StudentCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        student = serializer.save()
        student = (
            Student.objects.select_related("user", "institute", "home_hostel")
            .annotate(strike_count=Count("strikes", filter=Q(strikes__is_active=True)))
            .get(pk=student.pk)
        )
        return Response(
            AdminStudentSerializer(student, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, pk=None):
        student = self.get_object()
        serializer = StudentAssignSerializer(
            student, data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        student.refresh_from_db()
        return Response(AdminStudentSerializer(student, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="disable")
    def disable(self, request, pk=None):
        student = self.get_object()
        student.is_active = False
        student.save(update_fields=["is_active", "updated_at"])
        user = student.user
        user.is_active = False
        user.save(update_fields=["is_active", "updated_at"])
        student.refresh_from_db()
        audit(
            user=request.user,
            action=AuditAction.STUDENT_DISABLED,
            summary=f"Disabled {student.name}'s account ({user.email}).",
            target=student,
            target_type="student",
        )
        return Response(AdminStudentSerializer(student, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="enable")
    def enable(self, request, pk=None):
        student = self.get_object()
        student.is_active = True
        student.save(update_fields=["is_active", "updated_at"])
        user = student.user
        user.is_active = True
        user.save(update_fields=["is_active", "updated_at"])
        student.refresh_from_db()
        audit(
            user=request.user,
            action=AuditAction.STUDENT_ENABLED,
            summary=f"Re-enabled {student.name}'s account ({user.email}).",
            target=student,
            target_type="student",
        )
        return Response(AdminStudentSerializer(student, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="suspend")
    def suspend(self, request, pk=None):
        student = self.get_object()
        serializer = StudentSuspendSerializer(student, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        student.refresh_from_db()
        ends = timezone.localtime(student.suspension_ends).strftime("%Y-%m-%d %H:%M")
        reason = student.suspension_reason or "No reason provided."
        create_in_app_notification(
            student=student,
            title="Account suspended",
            body=f"Your account is suspended until {ends}. {reason}",
            notification_type=NotificationType.SUSPENSION,
            kind=NotificationKind.DANGER,
            related_object_type="student",
            related_object_id=student.id,
            preference_field="suspension",
        )
        return Response(AdminStudentSerializer(student, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="unsuspend")
    def unsuspend(self, request, pk=None):
        student = self.get_object()
        student.suspension_ends = None
        student.suspension_reason = ""
        student.save(update_fields=["suspension_ends", "suspension_reason", "updated_at"])
        student.refresh_from_db()
        return Response(AdminStudentSerializer(student, context={"request": request}).data)

    @action(detail=True, methods=["get", "post"], url_path="strikes")
    def strikes(self, request, pk=None):
        student = self.get_object()
        if request.method == "GET":
            qs = (
                Strike.objects.filter(student=student, is_active=True)
                .select_related("recorded_by", "ticket")
            )
            page = self.paginate_queryset(qs)
            serializer = StrikeSerializer(page if page is not None else qs, many=True)
            if page is not None:
                return self.get_paginated_response(serializer.data)
            return Response(serializer.data)

        serializer = StrikeSerializer(
            data=request.data, context={"request": request, "student": student}
        )
        serializer.is_valid(raise_exception=True)
        ticket = serializer.validated_data.get("ticket")
        if ticket is not None:
            scoped = scoped_institute_id(request.user)
            if scoped is not None and ticket.student.institute_id != scoped:
                return Response(
                    {"code": "PERMISSION_DENIED", "detail": "Ticket is outside your institute."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        strike = serializer.save(student=student, recorded_by=request.user)
        create_in_app_notification(
            student=student,
            title="Strike recorded",
            body=strike.reason,
            notification_type=NotificationType.STRIKE,
            kind=NotificationKind.WARN,
            related_object_type="strike",
            related_object_id=strike.id,
            preference_field="strike",
        )
        return Response(
            StrikeSerializer(strike, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="bookings")
    def bookings(self, request, pk=None):
        student = self.get_object()
        qs = (
            Booking.objects.filter(student=student, is_active=True)
            .select_related("machine", "machine__hostel")
            .order_by("-starts_at")
        )
        now = timezone.now()
        page = self.paginate_queryset(qs)
        rows = [serialize_booking_history(b, now=now) for b in (page if page is not None else qs)]
        serializer = StudentBookingHistoryItemSerializer(rows, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="promote")
    def promote(self, request, pk=None):
        student = self.get_object()
        user = student.user
        if user_is_super_administrator(user):
            return Response(
                {"detail": "Cannot promote a platform super-administrator."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            existing = user.administrator
        except ObjectDoesNotExist:
            existing = None
        if existing is not None and existing.is_active:
            return Response(
                {"detail": "Student is already an administrator."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if existing is not None:
            existing.is_active = True
            existing.display_name = student.name
            existing.institute = student.institute
            existing.save(
                update_fields=["is_active", "display_name", "institute", "updated_at"]
            )
            return Response(
                {
                    "detail": "Administrator role restored.",
                    "administrator_id": str(existing.id),
                    "student": AdminStudentSerializer(
                        student, context={"request": request}
                    ).data,
                }
            )

        admin = Administrator.objects.create(
            user=user,
            institute=student.institute,
            display_name=student.name,
        )
        return Response(
            {
                "detail": "Student promoted to administrator.",
                "administrator_id": str(admin.id),
                "student": AdminStudentSerializer(student, context={"request": request}).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="send-reset-link")
    def send_reset_link(self, request, pk=None):
        student = self.get_object()
        user = student.user
        email = user.email
        try:
            otp = create_otp(email, OtpPurpose.RESET, record_send=True)
            token = create_reset_link(user.id)
            send_password_reset_email_task.enqueue(to=email, otp=otp, token=token)
        except (OtpCooldown, OtpRateLimited):
            return Response(
                {"detail": "Unable to send reset link right now. Try again shortly."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        return Response({"detail": "Password reset link sent.", "email": email})

    @action(detail=False, methods=["post"], url_path="import")
    def import_students(self, request):
        upload = request.FILES.get("file") or request.FILES.get("csv")
        if upload is None:
            return Response(
                {"detail": "Upload a CSV file as `file` (or `csv`)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        scoped = scoped_institute_id(request.user)
        if scoped is not None:
            institute = Institute.objects.get(pk=scoped)
        else:
            institute_id = request.data.get("institute")
            if not institute_id:
                return Response(
                    {"detail": "Super administrators must pass `institute`."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                institute = Institute.objects.get(pk=institute_id)
            except Institute.DoesNotExist:
                return Response(
                    {"detail": "Institute not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

        try:
            raw = upload.read()
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return Response(
                {"detail": "CSV must be UTF-8 encoded."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return Response(
                {"detail": "CSV has no header row."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        field_map = {name.strip().lower(): name for name in reader.fieldnames if name}
        required = ("name", "email", "hostel", "gender")
        missing = [col for col in required if col not in field_map]
        if missing:
            return Response(
                {"detail": f"CSV missing required columns: {', '.join(missing)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        User = get_user_model()
        hostels = {
            h.name.strip().lower(): h
            for h in Hostel.objects.filter(institute=institute, is_active=True)
        }
        allowed = set(domains_of(institute))
        created, skipped, errors = [], [], []

        for index, row in enumerate(reader, start=2):
            name = (row.get(field_map["name"]) or "").strip()
            email = (row.get(field_map["email"]) or "").strip().lower()
            phone_key = field_map.get("phone")
            phone = (row.get(phone_key) or "").strip() if phone_key else ""
            hostel_name = (row.get(field_map["hostel"]) or "").strip()
            gender_raw = (row.get(field_map["gender"]) or "").strip().lower()

            if not email:
                errors.append({"row": index, "email": email, "detail": "Missing email."})
                continue
            if User.objects.filter(email__iexact=email).exists():
                skipped.append(
                    {"row": index, "email": email, "detail": "Already registered."}
                )
                continue

            domain = email_domain(email)
            if domain not in allowed:
                errors.append(
                    {
                        "row": index,
                        "email": email,
                        "detail": f"Domain '{domain}' is not allowed.",
                    }
                )
                continue

            hostel = hostels.get(hostel_name.lower())
            if hostel is None:
                errors.append(
                    {
                        "row": index,
                        "email": email,
                        "detail": f"Unknown hostel '{hostel_name}'.",
                    }
                )
                continue

            if gender_raw in ("m", "male"):
                gender = Gender.MALE
            elif gender_raw in ("f", "female"):
                gender = Gender.FEMALE
            else:
                errors.append(
                    {
                        "row": index,
                        "email": email,
                        "detail": f"Invalid gender '{gender_raw}'.",
                    }
                )
                continue

            if not name:
                errors.append({"row": index, "email": email, "detail": "Missing name."})
                continue

            try:
                with transaction.atomic():
                    user = User.objects.create_user(email=email, password=None)
                    user.set_unusable_password()
                    user.save(update_fields=["password", "updated_at"])
                    Student.objects.create(
                        user=user,
                        institute=institute,
                        name=name,
                        phone=phone,
                        gender=gender,
                        home_hostel=hostel,
                    )
                created.append({"row": index, "email": email, "name": name})
            except Exception as exc:  # noqa: BLE001 — collect per-row failures
                errors.append({"row": index, "email": email, "detail": str(exc)})

        return Response(
            {
                "created": len(created),
                "skipped": len(skipped),
                "errors": len(errors),
                "created_rows": created,
                "skipped_rows": skipped,
                "error_rows": errors,
            },
            status=status.HTTP_200_OK,
        )


class StrikeViewSet(
    InstituteScopedQuerysetMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Revoke strikes via POST ``revoke/`` or soft DELETE."""

    serializer_class = StrikeSerializer
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]
    queryset = Strike.objects.select_related(
        "student", "student__user", "recorded_by", "ticket"
    ).all()
    institute_filter = "student__institute"
    http_method_names = ["post", "delete", "head", "options"]

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None):
        strike = self.get_object()
        self.perform_destroy(strike)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SuspensionListView(APIView):
    """Active suspensions for the scoped institute."""

    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]
    serializer_class = SuspensionRowSerializer

    def get(self, request):
        now = timezone.now()
        qs = Student.objects.select_related("user").filter(suspension_ends__gt=now)
        if not user_is_super_administrator(request.user):
            institute_id = scoped_institute_id(request.user)
            if institute_id is None:
                qs = qs.none()
            else:
                qs = qs.filter(institute_id=institute_id)
        rows = [
            {
                "student_id": s.id,
                "student_name": s.name,
                "student_email": s.user.email,
                "until": timezone.localtime(s.suspension_ends).strftime("%Y-%m-%d"),
                "reason": s.suspension_reason or "",
                "by": "",
            }
            for s in qs.order_by("suspension_ends")
        ]
        return Response(SuspensionRowSerializer(rows, many=True).data)


class TicketViewSet(
    InstituteScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]
    queryset = Ticket.objects.select_related(
        "student",
        "student__user",
        "machine",
        "machine__hostel",
        "booking",
        "recorded_holder",
    ).prefetch_related("events")
    institute_filter = "student__institute"
    http_method_names = ["get", "patch", "head", "options"]

    def get_serializer_class(self):
        if self.action in ("update", "partial_update"):
            return AdminTicketUpdateSerializer
        return AdminTicketSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        status_filter = _param(self.request.query_params.get("status"))
        if status_filter:
            qs = qs.filter(status=status_filter)
        kind = _param(self.request.query_params.get("kind"))
        if kind:
            qs = qs.filter(kind=kind)
        hostel_id = _param(
            self.request.query_params.get("hostelId"),
            self.request.query_params.get("hostel"),
        )
        if hostel_id:
            qs = qs.filter(machine__hostel_id=hostel_id)
        return qs

    def partial_update(self, request, *args, **kwargs):
        ticket = self.get_object()
        serializer = AdminTicketUpdateSerializer(ticket, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        old_status = ticket.status
        old_note = ticket.committee_note
        ticket = serializer.save()
        now = timezone.now()

        if old_status != ticket.status:
            if ticket.status == TicketStatus.RESOLVED:
                ticket.resolved_at = now
                ticket.save(update_fields=["resolved_at", "updated_at"])
                TicketEvent.objects.create(
                    ticket=ticket,
                    title="Resolved",
                    note=ticket.committee_note,
                    actor=request.user,
                )
            elif ticket.status == TicketStatus.OPEN:
                ticket.resolved_at = None
                ticket.save(update_fields=["resolved_at", "updated_at"])
                TicketEvent.objects.create(
                    ticket=ticket,
                    title="Reopened",
                    note=ticket.committee_note,
                    actor=request.user,
                )
            number = f"#{ticket.number}" if ticket.number is not None else str(ticket.id)[:8]
            create_in_app_notification(
                student=ticket.student,
                title="Ticket update",
                body=f"Ticket {number} is now {ticket.status}.",
                notification_type=NotificationType.TICKET_UPDATE,
                kind=NotificationKind.INFO,
                related_object_type="ticket",
                related_object_id=ticket.id,
                preference_field="ticket_update",
            )
        elif old_note != ticket.committee_note and ticket.committee_note:
            TicketEvent.objects.create(
                ticket=ticket,
                title="Committee note",
                note=ticket.committee_note,
                actor=request.user,
            )

        ticket = (
            Ticket.objects.select_related(
                "student",
                "student__user",
                "machine",
                "machine__hostel",
                "booking",
                "recorded_holder",
            )
            .prefetch_related("events")
            .get(pk=ticket.pk)
        )
        return Response(AdminTicketSerializer(ticket, context={"request": request}).data)
