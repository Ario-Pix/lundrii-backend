"""Admin portal: audit log, dashboard, impact previews, own password."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from base.apidocs import error_table
from base.exceptions import APIError
from base.permissions import IsAdministratorOrSuperAdministrator
from laundry.models import AdminAuditLog, Machine
from laundry.permissions import scoped_institute_id
from laundry.serializers.admin_ops import (
    ActivityItemSerializer,
    AttentionItemSerializer,
    AuditEntrySerializer,
    ChangePasswordSerializer,
    DashboardSummarySerializer,
    ImpactPreviewSerializer,
    MachineHoursImpactRequestSerializer,
)
from laundry.services import admin_ops
from laundry.services.audit import Action, record


def _parse_date(raw: str | None) -> date:
    if not raw:
        return timezone.localdate()
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise APIError(
            "VALIDATION_ERROR",
            detail="Invalid date. Use YYYY-MM-DD.",
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from exc


class AdminOpsMixin:
    permission_classes = [IsAuthenticated, IsAdministratorOrSuperAdministrator]

    @property
    def institute_id(self):
        return scoped_institute_id(self.request.user)

    def machine_or_404(self, machine_id) -> Machine:
        qs = Machine.objects.filter(pk=machine_id, is_active=True).select_related(
            "hostel"
        )
        institute_id = self.institute_id
        if institute_id is not None:
            qs = qs.filter(hostel__institute_id=institute_id)
        machine = qs.first()
        if machine is None:
            raise APIError(
                "NOT_FOUND",
                detail="Machine not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return machine


@extend_schema(
    summary="Administrator action log",
    description=(
        "Append-only record of every administrator action: who did what, to "
        "whom or what, and when. Nothing here can be edited or deleted.\n\n"
        "Actor and target names are frozen as they were at the time, so an "
        "entry still reads correctly after a machine is retired or an "
        "administrator is renamed.\n\n" + error_table("AUTHENTICATION_FAILED", "PERMISSION_DENIED")
    ),
    parameters=[
        OpenApiParameter("administrator", str, description="Administrator UUID."),
        OpenApiParameter("action", str, description="Action key, e.g. machine.offline."),
        OpenApiParameter("date_from", str, description="Inclusive YYYY-MM-DD."),
        OpenApiParameter("date_to", str, description="Inclusive YYYY-MM-DD."),
    ],
)
class AuditLogView(AdminOpsMixin, ListAPIView):
    serializer_class = AuditEntrySerializer

    def get_queryset(self):
        qs = AdminAuditLog.objects.select_related("actor").all()
        institute_id = self.institute_id
        if institute_id is not None:
            qs = qs.filter(institute_id=institute_id)

        params = self.request.query_params
        administrator = params.get("administrator")
        if administrator:
            try:
                qs = qs.filter(actor_id=UUID(str(administrator)))
            except (ValueError, TypeError) as exc:
                raise APIError(
                    "VALIDATION_ERROR", detail="administrator must be a UUID."
                ) from exc
        action = params.get("action")
        if action:
            qs = qs.filter(action=action)

        tz = timezone.get_current_timezone()
        if params.get("date_from"):
            start = timezone.make_aware(
                datetime.combine(_parse_date(params["date_from"]), datetime.min.time()), tz
            )
            qs = qs.filter(created_at__gte=start)
        if params.get("date_to"):
            end = timezone.make_aware(
                datetime.combine(_parse_date(params["date_to"]), datetime.max.time()), tz
            )
            qs = qs.filter(created_at__lte=end)
        return qs


@extend_schema(
    summary="Dashboard headline numbers",
    description=(
        "Bookings, capacity use, open tickets and machine availability for one "
        "day. Capacity counts only the slots that online machines actually "
        "offer that day — an offline machine offers none, so it must not "
        "inflate the denominator and make utilisation look low."
    ),
    parameters=[
        OpenApiParameter("date", str, description="YYYY-MM-DD, defaults to today."),
        OpenApiParameter("hostel", str, description="Hostel UUID or 'all'."),
    ],
    responses=DashboardSummarySerializer,
)
class DashboardSummaryView(AdminOpsMixin, APIView):
    def get(self, request):
        data = admin_ops.dashboard_summary(
            institute_id=self.institute_id,
            on_date=_parse_date(request.query_params.get("date")),
            hostel_id=request.query_params.get("hostel"),
        )
        return Response(DashboardSummarySerializer(data).data)


@extend_schema(
    summary="Things that need attention",
    description=(
        "Machines with open maintenance reports, and machines that have been "
        "offline a while — worst first. Each "
        "item carries the target so the portal can link straight to where it "
        "is handled."
    ),
    parameters=[OpenApiParameter("hostel", str, description="Hostel UUID or 'all'.")],
    responses=AttentionItemSerializer(many=True),
)
class AttentionView(AdminOpsMixin, APIView):
    def get(self, request):
        items = admin_ops.needs_attention(
            institute_id=self.institute_id,
            hostel_id=request.query_params.get("hostel"),
        )
        return Response(AttentionItemSerializer(items, many=True).data)


@extend_schema(
    summary="Recent activity",
    description=(
        "Bookings, cancellations and administrator actions merged into one "
        "timeline, newest first."
    ),
    parameters=[
        OpenApiParameter("hostel", str, description="Hostel UUID or 'all'."),
        OpenApiParameter("limit", int, description="Max items (default 30, max 100)."),
    ],
    responses=ActivityItemSerializer(many=True),
)
class ActivityView(AdminOpsMixin, APIView):
    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get("limit", 30)), 1), 100)
        except (TypeError, ValueError):
            limit = 30
        items = admin_ops.recent_activity(
            institute_id=self.institute_id,
            hostel_id=request.query_params.get("hostel"),
            limit=limit,
        )
        return Response(ActivityItemSerializer(items, many=True).data)


@extend_schema(
    summary="What taking this machine offline would cancel",
    description=(
        "Preview, before committing. Returns every upcoming booking that "
        "`POST /admin/machines/{id}/offline/` would cancel and how many "
        "students would be notified. The preview and the action run the same "
        "query, so what is shown is what happens."
    ),
    responses=ImpactPreviewSerializer,
)
class MachineOfflineImpactView(AdminOpsMixin, APIView):
    def get(self, request, machine_id: UUID):
        machine = self.machine_or_404(machine_id)
        return Response(ImpactPreviewSerializer(admin_ops.offline_impact(machine)).data)


@extend_schema(
    summary="What narrowing this machine's hours would strand",
    description=(
        "Preview, before committing. Returns the upcoming bookings that would "
        "fall outside a proposed operating window, so the administrator can "
        "choose explicitly whether to cancel them or keep them — "
        "`PATCH /admin/machines/{id}/hours/` takes `cancel_outside`."
    ),
    request=MachineHoursImpactRequestSerializer,
    responses=ImpactPreviewSerializer,
)
class MachineHoursImpactView(AdminOpsMixin, APIView):
    def post(self, request, machine_id: UUID):
        machine = self.machine_or_404(machine_id)
        serializer = MachineHoursImpactRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        impact = admin_ops.hours_change_impact(
            machine,
            serializer.validated_data["operating_window_start"],
            serializer.validated_data["operating_window_end"],
        )
        return Response(ImpactPreviewSerializer(impact).data)


@extend_schema(
    summary="Change your own password",
    description=(
        "An administrator changing their own password. The current password is "
        "required — a hijacked session must not be able to lock the real owner "
        "out.\n\n" + error_table("VALIDATION_ERROR", "AUTHENTICATION_FAILED")
    ),
    request=ChangePasswordSerializer,
    responses={204: OpenApiResponse(description="Password changed.")},
)
class AdminChangePasswordView(AdminOpsMixin, APIView):
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user

        if not user.check_password(serializer.validated_data["current_password"]):
            raise APIError(
                "AUTHENTICATION_FAILED",
                detail="Current password is incorrect.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password", "updated_at"])
        record(
            user=user,
            action=Action.ADMIN_PASSWORD_CHANGED,
            summary=f"{user.email} changed their own password.",
            target_type="administrator",
            target_label=user.email,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
