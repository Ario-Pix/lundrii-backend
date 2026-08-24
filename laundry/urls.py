"""Laundry API routes (Wave 2+)."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from laundry.views import exchanges as exchange_views
from laundry.views import me as me_views
from laundry.views import student as student_views
from laundry.views import tickets as ticket_views
from laundry.views import admin_bookings as admin_booking_views
from laundry.views import admin_ops as admin_ops_views
from laundry.views.admin import (
    AdminProfileView,
    HostelViewSet,
    InstituteRuleViewSet,
    InstituteViewSet,
    MachineViewSet,
    StrikeViewSet,
    StudentViewSet,
    SuspensionListView,
    TicketViewSet,
)

admin_router = DefaultRouter()
admin_router.register(r"institutes", InstituteViewSet, basename="admin-institute")
admin_router.register(r"hostels", HostelViewSet, basename="admin-hostel")
admin_router.register(r"machines", MachineViewSet, basename="admin-machine")
admin_router.register(r"rules", InstituteRuleViewSet, basename="admin-rule")
admin_router.register(r"students", StudentViewSet, basename="admin-student")
admin_router.register(r"strikes", StrikeViewSet, basename="admin-strike")
admin_router.register(r"tickets", TicketViewSet, basename="admin-ticket")

urlpatterns = [
    path("admin/me/", AdminProfileView.as_view(), name="admin-me"),
    path("admin/profile/", AdminProfileView.as_view(), name="admin-profile"),
    path("admin/suspensions/", SuspensionListView.as_view(), name="admin-suspensions"),
    # Track E — admin bookings grid + analytics (before router catch-alls).
    path(
        "admin/bookings/grid",
        admin_booking_views.AdminBookingGridView.as_view(),
        name="admin-bookings-grid",
    ),
    path(
        "admin/bookings/export.csv",
        admin_booking_views.AdminBookingExportCsvView.as_view(),
        name="admin-bookings-export-csv",
    ),
    path(
        "admin/bookings/<uuid:booking_id>/",
        admin_booking_views.AdminBookingDetailView.as_view(),
        name="admin-booking-detail",
    ),
    path(
        "admin/bookings/<uuid:booking_id>/cancel/",
        admin_booking_views.AdminBookingCancelView.as_view(),
        name="admin-booking-cancel",
    ),
    path(
        "admin/analytics/demand-by-hour",
        admin_booking_views.AdminDemandByHourView.as_view(),
        name="admin-analytics-demand-by-hour",
    ),
    path(
        "admin/analytics/weekday-shape",
        admin_booking_views.AdminWeekdayShapeView.as_view(),
        name="admin-analytics-weekday-shape",
    ),
    path(
        "admin/analytics/channel-shares",
        admin_booking_views.AdminChannelSharesView.as_view(),
        name="admin-analytics-channel-shares",
    ),
    # Admin portal operations. Declared before the router so the literal
    # segments are not swallowed by its detail routes.
    path(
        "admin/audit-log",
        admin_ops_views.AuditLogView.as_view(),
        name="admin-audit-log",
    ),
    path(
        "admin/dashboard/summary",
        admin_ops_views.DashboardSummaryView.as_view(),
        name="admin-dashboard-summary",
    ),
    path(
        "admin/dashboard/attention",
        admin_ops_views.AttentionView.as_view(),
        name="admin-dashboard-attention",
    ),
    path(
        "admin/activity",
        admin_ops_views.ActivityView.as_view(),
        name="admin-activity",
    ),
    path(
        "admin/machines/<uuid:machine_id>/offline-impact",
        admin_ops_views.MachineOfflineImpactView.as_view(),
        name="admin-machine-offline-impact",
    ),
    path(
        "admin/machines/<uuid:machine_id>/hours-impact",
        admin_ops_views.MachineHoursImpactView.as_view(),
        name="admin-machine-hours-impact",
    ),
    path(
        "admin/me/change-password",
        admin_ops_views.AdminChangePasswordView.as_view(),
        name="admin-change-password",
    ),
    path("admin/", include(admin_router.urls)),
    # Student mobile routes (Wave 2c) — do not remove admin routes above.
    path(
        "home",
        student_views.HomeView.as_view(),
        name="student-home",
    ),
    path(
        "hostels/<uuid:hostel_id>/machines",
        student_views.HostelMachineListView.as_view(),
        name="student-hostel-machines",
    ),
    path(
        "hostels/<uuid:hostel_id>/availability/now",
        student_views.HostelAvailabilityNowView.as_view(),
        name="student-hostel-availability-now",
    ),
    path(
        "machines/<uuid:machine_id>",
        student_views.MachineDetailView.as_view(),
        name="student-machine-detail",
    ),
    path(
        "machines/<uuid:machine_id>/slots",
        student_views.MachineSlotListView.as_view(),
        name="student-machine-slots",
    ),
    path(
        "availability/misses",
        student_views.AvailabilityMissCreateView.as_view(),
        name="student-availability-misses",
    ),
    path(
        "bookings",
        student_views.BookingListCreateView.as_view(),
        name="student-bookings",
    ),
    path(
        "bookings/<uuid:booking_id>",
        student_views.BookingDetailView.as_view(),
        name="student-booking-detail",
    ),
    path(
        "bookings/<uuid:booking_id>/cancel",
        student_views.BookingCancelView.as_view(),
        name="student-booking-cancel",
    ),
    path(
        "bookings/<uuid:booking_id>/move",
        student_views.BookingMoveView.as_view(),
        name="student-booking-move",
    ),
    path(
        "bookings/<uuid:booking_id>/move-options",
        student_views.BookingMoveOptionsView.as_view(),
        name="student-booking-move-options",
    ),
    # Student tickets (Wave 3b) — append only; do not replace admin/tickets.
    path(
        "tickets",
        ticket_views.StudentTicketListCreateView.as_view(),
        name="student-tickets",
    ),
    path(
        "tickets/<uuid:ticket_id>",
        ticket_views.StudentTicketDetailView.as_view(),
        name="student-ticket-detail",
    ),
    # Student exchanges (Wave 3a) — do not remove admin/booking/ticket routes.
    path(
        "exchanges",
        exchange_views.ExchangeListCreateView.as_view(),
        name="student-exchanges",
    ),
    path(
        "exchanges/<uuid:exchange_id>/approve",
        exchange_views.ExchangeApproveView.as_view(),
        name="student-exchange-approve",
    ),
    path(
        "exchanges/<uuid:exchange_id>/reject",
        exchange_views.ExchangeRejectView.as_view(),
        name="student-exchange-reject",
    ),
    path(
        "exchanges/<uuid:exchange_id>/withdraw",
        exchange_views.ExchangeWithdrawView.as_view(),
        name="student-exchange-withdraw",
    ),
    # Student profile + notifications (Wave 3c)
    path("me", me_views.MeView.as_view(), name="student-me"),
    path("me/hostels", me_views.MeHostelsView.as_view(), name="student-me-hostels"),
    path("me/institute", me_views.MeInstituteView.as_view(), name="student-me-institute"),
    path(
        "notifications",
        me_views.NotificationListView.as_view(),
        name="student-notifications",
    ),
    path(
        "notifications/read-all",
        me_views.NotificationReadAllView.as_view(),
        name="student-notifications-read-all",
    ),
    path(
        "notifications/preferences",
        me_views.NotificationPreferencesView.as_view(),
        name="student-notification-preferences",
    ),
    path(
        "notifications/<uuid:notification_id>/read",
        me_views.NotificationReadView.as_view(),
        name="student-notification-read",
    ),
]
