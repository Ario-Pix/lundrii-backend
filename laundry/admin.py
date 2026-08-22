from django.contrib import admin

from laundry.models import (
    Administrator,
    AvailabilityMiss,
    Booking,
    Exchange,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    Notification,
    NotificationPreference,
    Strike,
    Student,
    SuperAdministrator,
    Ticket,
    TicketEvent,
)


@admin.register(Institute)
class InstituteAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "created_at")
    search_fields = ("name",)
    list_filter = ("is_active",)


@admin.register(InstituteRule)
class InstituteRuleAdmin(admin.ModelAdmin):
    list_display = (
        "institute",
        "quota_limit",
        "quota_window_days",
        "cooldown_hours",
        "advance_window_days",
        "cancellation_cutoff_hours",
        "dryer_cap_enabled",
    )
    list_filter = ("dryer_cap_enabled",)
    raw_id_fields = ("institute",)


@admin.register(Hostel)
class HostelAdmin(admin.ModelAdmin):
    list_display = ("name", "institute", "gender", "is_active")
    list_filter = ("gender", "institute", "is_active")
    search_fields = ("name",)
    raw_id_fields = ("institute",)


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "user",
        "institute",
        "home_hostel",
        "floor",
        "gender",
        "email_verified_at",
        "suspension_ends",
        "is_active",
    )
    list_filter = ("gender", "institute", "is_active")
    search_fields = ("name", "user__email", "phone")
    raw_id_fields = ("user", "institute", "home_hostel")


@admin.register(Administrator)
class AdministratorAdmin(admin.ModelAdmin):
    list_display = ("display_name", "user", "institute", "is_active")
    list_filter = ("institute", "is_active")
    search_fields = ("display_name", "user__email")
    raw_id_fields = ("user", "institute")


@admin.register(SuperAdministrator)
class SuperAdministratorAdmin(admin.ModelAdmin):
    list_display = ("display_name", "user", "is_active")
    search_fields = ("display_name", "user__email")
    raw_id_fields = ("user",)


@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_display = (
        "location_name",
        "kind",
        "hostel",
        "slot_length_minutes",
        "is_offline",
        "is_active",
    )
    list_filter = ("kind", "is_offline", "is_active", "hostel__institute")
    search_fields = ("location_name",)
    raw_id_fields = ("hostel",)


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = (
        "student",
        "machine",
        "starts_at",
        "ends_at",
        "channel",
        "cancelled_at",
        "counts_against_quota",
        "is_late_cancel",
    )
    list_filter = ("channel", "counts_against_quota", "is_late_cancel")
    search_fields = ("student__name", "student__user__email")
    raw_id_fields = ("student", "machine")


@admin.register(Exchange)
class ExchangeAdmin(admin.ModelAdmin):
    list_display = ("kind", "status", "requester", "holder", "target_booking", "created_at")
    list_filter = ("kind", "status")
    raw_id_fields = ("requester", "holder", "target_booking", "offered_booking")


class TicketEventInline(admin.TabularInline):
    model = TicketEvent
    extra = 0
    raw_id_fields = ("actor",)
    readonly_fields = ("created_at",)


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("number", "kind", "status", "student", "machine", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("number", "student__name", "student_note")
    raw_id_fields = ("student", "machine", "booking", "recorded_holder")
    inlines = (TicketEventInline,)


@admin.register(Strike)
class StrikeAdmin(admin.ModelAdmin):
    list_display = ("student", "date", "recorded_by", "ticket")
    list_filter = ("date",)
    search_fields = ("student__name", "reason")
    raw_id_fields = ("student", "recorded_by", "ticket")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "student", "type", "kind", "read_at", "created_at")
    list_filter = ("type", "kind")
    search_fields = ("title", "student__name")
    raw_id_fields = ("student",)


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("student", "booking_confirmed", "slot_reminder", "exchange_request")
    raw_id_fields = ("student",)


@admin.register(AvailabilityMiss)
class AvailabilityMissAdmin(admin.ModelAdmin):
    list_display = ("student", "machine", "date", "hour")
    list_filter = ("date",)
    raw_id_fields = ("student", "machine")
