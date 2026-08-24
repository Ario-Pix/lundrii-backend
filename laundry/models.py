"""
Laundry domain models.

All concrete models inherit ``base.BaseModel`` (UUID pk, timestamps, is_active).
Role profiles are OneToOne to ``base.BaseUser``.
"""

from datetime import time

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from base.models import BaseModel


class Gender(models.TextChoices):
    MALE = "male", "Male"
    FEMALE = "female", "Female"


class MachineKind(models.TextChoices):
    WASHER = "washer", "Washer"
    DRYER = "dryer", "Dryer"


class ExchangeKind(models.TextChoices):
    REQUEST = "request", "Request"
    SWAP = "swap", "Swap"


class ExchangeStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"
    FAILED = "failed", "Failed"


class TicketKind(models.TextChoices):
    # Legacy. New tickets are maintenance only; POST kind=conflict is rejected.
    CONFLICT = "conflict", "Conflict"
    MAINTENANCE = "maintenance", "Maintenance"


class TicketStatus(models.TextChoices):
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


class NotificationKind(models.TextChoices):
    INFO = "info", "Info"
    WARN = "warn", "Warn"
    DANGER = "danger", "Danger"
    SUCCESS = "success", "Success"


class NotificationType(models.TextChoices):
    BOOKING_CONFIRMED = "booking_confirmed", "Booking confirmed"
    SLOT_REMINDER = "slot_reminder", "Upcoming slot reminder"
    BOOKING_CANCELLED_OFFLINE = "booking_cancelled_offline", "Booking cancelled (machine offline)"
    EXCHANGE_REQUEST = "exchange_request", "Exchange request"
    EXCHANGE_OUTCOME = "exchange_outcome", "Exchange outcome"
    TICKET_UPDATE = "ticket_update", "Ticket update"
    STRIKE = "strike", "Strike"
    SUSPENSION = "suspension", "Suspension"


# ---------------------------------------------------------------------------
# Organisation
# ---------------------------------------------------------------------------


class Institute(BaseModel):
    name = models.CharField(max_length=200, unique=True)
    allowed_email_domains = models.JSONField(
        default=list,
        help_text='Allowed email domains, e.g. ["gim.ac.in", "student.gim.ac.in"].',
    )

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class InstituteRule(BaseModel):
    """Fairness rules for an institute. One set per institute."""

    institute = models.OneToOneField(
        Institute,
        on_delete=models.CASCADE,
        related_name="rules",
    )
    quota_limit = models.PositiveIntegerField(
        default=3,
        help_text="Max washer bookings counted against quota Monday to Sunday.",
    )
    quota_window_days = models.PositiveIntegerField(
        default=7,
        help_text="Kept for API compatibility. Quota is always a Monday–Sunday week.",
    )
    cooldown_hours = models.PositiveIntegerField(
        default=0,
        help_text="Unused. Booking does not require a gap between washes.",
    )
    advance_window_days = models.PositiveIntegerField(
        default=7,
        help_text="How many days ahead a student may book.",
    )
    cancellation_cutoff_hours = models.PositiveIntegerField(
        default=6,
        help_text="Cancelling later than this many hours before start still counts against quota.",
    )
    dryer_cap_enabled = models.BooleanField(
        default=True,
        help_text=(
            "If enabled, dryer bookings have a separate weekly cap equal to "
            "quota_limit (same Monday–Sunday window). Dryers never consume "
            "washer quota."
        ),
    )

    class Meta:
        verbose_name = "institute rule"
        verbose_name_plural = "institute rules"

    def __str__(self) -> str:
        return f"Rules for {self.institute}"


class Hostel(BaseModel):
    institute = models.ForeignKey(
        Institute,
        on_delete=models.PROTECT,
        related_name="hostels",
    )
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ("institute", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("institute", "name"),
                name="uniq_hostel_name_per_institute",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.institute})"


# ---------------------------------------------------------------------------
# Roles (OneToOne → BaseUser)
# ---------------------------------------------------------------------------


class Student(BaseModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="student",
    )
    institute = models.ForeignKey(
        Institute,
        on_delete=models.PROTECT,
        related_name="students",
    )
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=32, blank=True)
    whatsapp_opt_in = models.BooleanField(default=False)
    gender = models.CharField(
        max_length=16,
        choices=Gender.choices,
        blank=True,
        help_text="Assigned by an administrator, not chosen by the student.",
    )
    home_hostel = models.ForeignKey(
        Hostel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="residents",
        help_text="Chosen at sign-up, or assigned by an administrator.",
    )
    floor = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Unused. Machines belong to a hostel, not to a student.",
    )
    email_verified_at = models.DateTimeField(null=True, blank=True)
    suspension_ends = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.TextField(blank=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return f"{self.name} <{self.user.email}>"

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_suspended(self) -> bool:
        if self.suspension_ends is None:
            return False
        return timezone.now() < self.suspension_ends


class Administrator(BaseModel):
    """Hostel committee member — scoped to one institute."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="administrator",
    )
    institute = models.ForeignKey(
        Institute,
        on_delete=models.PROTECT,
        related_name="administrators",
    )
    display_name = models.CharField(max_length=150)

    class Meta:
        ordering = ("display_name",)
        verbose_name = "administrator"
        verbose_name_plural = "administrators"

    def __str__(self) -> str:
        return f"{self.display_name} ({self.institute})"


class SuperAdministrator(BaseModel):
    """Platform operator. Provisions institutes; no student-facing UI."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="superadministrator",
    )
    display_name = models.CharField(max_length=150)

    class Meta:
        verbose_name = "super administrator"
        verbose_name_plural = "super administrators"

    def __str__(self) -> str:
        return self.display_name


# ---------------------------------------------------------------------------
# Machines & bookings
# ---------------------------------------------------------------------------


class Machine(BaseModel):
    hostel = models.ForeignKey(
        Hostel,
        on_delete=models.CASCADE,
        related_name="machines",
    )
    kind = models.CharField(max_length=16, choices=MachineKind.choices)
    location_name = models.CharField(
        max_length=200,
        help_text='Human-readable location, e.g. "3rd Floor · A Wing".',
    )
    operating_window_start = models.TimeField(
        default=time(0, 0),
        help_text="Inclusive. Equal to end means 24-hour operation.",
    )
    operating_window_end = models.TimeField(
        default=time(0, 0),
        help_text="Exclusive upper bound of the operating window.",
    )
    slot_length_minutes = models.PositiveIntegerField(default=60)
    is_offline = models.BooleanField(default=False)

    class Meta:
        ordering = ("hostel", "kind", "location_name")

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.location_name}"


class BookingChannel(models.TextChoices):
    """
    Where a booking came from.

    Resolved per request by ``base.clients.resolve_channel`` — never sent in a
    request body. ``APP`` is the fallback for a caller that did not identify
    itself, which is also every booking made before channel detection shipped.
    """

    APP = "app", "App"
    ANDROID = "android", "Android app"
    IOS = "ios", "iOS app"
    WHATSAPP = "whatsapp", "WhatsApp"
    WEBSITE = "website", "Website"
    MCP = "mcp", "Assistant (MCP)"


class Booking(BaseModel):
    """
    One student holding one machine for one derived slot (start/end datetimes).

    Partial unique constraint ``uniq_active_booking_machine_start``: at most one
    non-cancelled, ``is_active`` booking per (machine, starts_at). Django emits a
    partial unique index; SQLite 3.8+ supports this. Concurrent claims should
    still catch IntegrityError in the booking service (Wave 2c).
    """

    student = models.ForeignKey(
        Student,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    machine = models.ForeignKey(
        Machine,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    cancelled_at = models.DateTimeField(null=True, blank=True)
    counts_against_quota = models.BooleanField(default=True)
    is_late_cancel = models.BooleanField(
        default=False,
        help_text="True when cancelled inside the institute cutoff; still counts against quota.",
    )
    channel = models.CharField(
        max_length=32,
        choices=BookingChannel.choices,
        default=BookingChannel.APP,
        help_text='Booking source channel; default "app" until multi-channel clients exist.',
    )

    class Meta:
        ordering = ("-starts_at",)
        indexes = [
            models.Index(fields=("machine", "starts_at")),
            models.Index(fields=("student", "starts_at")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("machine", "starts_at"),
                condition=models.Q(cancelled_at__isnull=True, is_active=True),
                name="uniq_active_booking_machine_start",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student} @ {self.machine} {self.starts_at:%Y-%m-%d %H:%M}"

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None


class Exchange(BaseModel):
    kind = models.CharField(max_length=16, choices=ExchangeKind.choices)
    status = models.CharField(
        max_length=16,
        choices=ExchangeStatus.choices,
        default=ExchangeStatus.PENDING,
    )
    requester = models.ForeignKey(
        Student,
        on_delete=models.PROTECT,
        related_name="exchanges_sent",
    )
    holder = models.ForeignKey(
        Student,
        on_delete=models.PROTECT,
        related_name="exchanges_received",
        help_text="Current holder of the target booking (denormalized for queries).",
    )
    target_booking = models.ForeignKey(
        Booking,
        on_delete=models.PROTECT,
        related_name="exchanges_as_target",
    )
    offered_booking = models.ForeignKey(
        Booking,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="exchanges_as_offer",
        help_text="Set when kind is swap.",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(
        blank=True,
        help_text="Why an approved exchange failed rule checks.",
    )
    reject_note = models.TextField(
        blank=True,
        help_text="Optional note from the holder when declining a request.",
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("holder", "status")),
            models.Index(fields=("requester", "status")),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.status} → {self.target_booking_id}"


class AvailabilityMiss(BaseModel):
    """Recorded when a student wanted a slot and nothing was free (capacity signal)."""

    student = models.ForeignKey(
        Student,
        on_delete=models.PROTECT,
        related_name="availability_misses",
    )
    machine = models.ForeignKey(
        Machine,
        on_delete=models.PROTECT,
        related_name="availability_misses",
    )
    date = models.DateField()
    hour = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(23)],
        help_text="Hour of day in 0–23 (institute-local; stored as requested).",
    )

    class Meta:
        ordering = ("-date", "-hour")
        verbose_name = "availability miss"
        verbose_name_plural = "availability misses"
        constraints = [
            models.UniqueConstraint(
                fields=("student", "machine", "date", "hour"),
                name="uniq_availability_miss_student_slot",
            ),
            models.CheckConstraint(
                condition=models.Q(hour__gte=0) & models.Q(hour__lte=23),
                name="availabilitymiss_hour_0_23",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student_id} miss {self.machine_id} {self.date} {self.hour:02d}:00"


# ---------------------------------------------------------------------------
# Tickets & strikes
# ---------------------------------------------------------------------------


class Ticket(BaseModel):
    student = models.ForeignKey(
        Student,
        on_delete=models.PROTECT,
        related_name="tickets",
    )
    kind = models.CharField(max_length=16, choices=TicketKind.choices)
    status = models.CharField(
        max_length=16,
        choices=TicketStatus.choices,
        default=TicketStatus.OPEN,
    )
    number = models.PositiveIntegerField(
        unique=True,
        null=True,
        blank=True,
        help_text="Display number (e.g. 427 → #427). Assigned by services.",
    )
    machine = models.ForeignKey(
        Machine,
        on_delete=models.PROTECT,
        related_name="tickets",
        help_text="The machine this report is about.",
    )
    booking = models.ForeignKey(
        Booking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
        help_text="Unused. Kept for older rows; new tickets do not attach a booking.",
    )
    recorded_holder = models.ForeignKey(
        Student,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets_as_recorded_holder",
        help_text="Unused. Kept for older rows; new tickets do not attach a holder.",
    )
    slot_start = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Unused. Kept for older rows; new tickets do not attach a slot.",
    )
    student_note = models.TextField(blank=True)
    photo_url = models.URLField(max_length=500, blank=True)
    committee_note = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        label = f"#{self.number}" if self.number is not None else str(self.id)
        return f"{label} {self.get_kind_display()} ({self.status})"


class TicketEvent(BaseModel):
    """Timeline step on a ticket (raised → seen → resolved, plus notes)."""

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="events",
    )
    title = models.CharField(max_length=120)
    note = models.TextField(blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_events",
    )
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("occurred_at",)

    def __str__(self) -> str:
        return f"{self.ticket_id}: {self.title}"


class Strike(BaseModel):
    student = models.ForeignKey(
        Student,
        on_delete=models.PROTECT,
        related_name="strikes",
    )
    reason = models.TextField()
    date = models.DateField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="strikes_recorded",
    )
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="strikes",
    )

    class Meta:
        ordering = ("-date", "-created_at")

    def __str__(self) -> str:
        return f"Strike {self.student_id} on {self.date}"


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class Notification(BaseModel):
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    title = models.CharField(max_length=200)
    body = models.TextField()
    type = models.CharField(max_length=40, choices=NotificationType.choices)
    kind = models.CharField(
        max_length=16,
        choices=NotificationKind.choices,
        default=NotificationKind.INFO,
    )
    read_at = models.DateTimeField(null=True, blank=True)
    related_object_type = models.CharField(
        max_length=40,
        blank=True,
        help_text="Optional target type: booking, exchange, ticket, strike.",
    )
    related_object_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("student", "-created_at")),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class NotificationPreference(BaseModel):
    student = models.OneToOneField(
        Student,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    booking_confirmed = models.BooleanField(default=True)
    slot_reminder = models.BooleanField(default=True)
    booking_cancelled_offline = models.BooleanField(default=True)
    exchange_request = models.BooleanField(default=True)
    exchange_outcome = models.BooleanField(default=True)
    ticket_update = models.BooleanField(default=True)
    strike = models.BooleanField(default=True)
    suspension = models.BooleanField(default=True)

    class Meta:
        verbose_name = "notification preference"
        verbose_name_plural = "notification preferences"

    def __str__(self) -> str:
        return f"Preferences for {self.student_id}"


class AdminAuditLog(BaseModel):
    """
    An append-only record of what administrators did.

    Deliberately denormalised: ``actor_label`` and ``target_label`` are copied
    in at write time rather than joined at read time. The log has to stay
    readable after the administrator who acted is renamed or the machine they
    took offline is retired — a log that rewrites itself when the world moves on
    is not a log.

    Nothing updates or deletes these rows. ``is_active`` is inherited from
    BaseModel and left alone.
    """

    class Action(models.TextChoices):
        MACHINE_OFFLINE = "machine.offline", "Machine taken offline"
        MACHINE_ONLINE = "machine.online", "Machine brought online"
        MACHINE_HOURS = "machine.hours", "Machine hours changed"
        MACHINE_CREATED = "machine.created", "Machine added"
        MACHINE_UPDATED = "machine.updated", "Machine updated"
        HOSTEL_CREATED = "hostel.created", "Hostel added"
        HOSTEL_UPDATED = "hostel.updated", "Hostel updated"
        RULES_UPDATED = "rules.updated", "Booking rules changed"
        STUDENT_CREATED = "student.created", "Student added"
        STUDENT_ASSIGNED = "student.assigned", "Student hostel/gender changed"
        STUDENT_DISABLED = "student.disabled", "Student account disabled"
        STUDENT_ENABLED = "student.enabled", "Student account re-enabled"
        STUDENT_PROMOTED = "student.promoted", "Student promoted to administrator"
        STUDENT_RESET_SENT = "student.reset_sent", "Password reset link sent"
        STUDENT_IMPORTED = "student.imported", "Students imported"
        STRIKE_RECORDED = "strike.recorded", "Strike recorded"
        STRIKE_REVOKED = "strike.revoked", "Strike revoked"
        SUSPENSION_ADDED = "suspension.added", "Student suspended"
        SUSPENSION_LIFTED = "suspension.lifted", "Suspension lifted"
        TICKET_RESOLVED = "ticket.resolved", "Ticket resolved"
        TICKET_UPDATED = "ticket.updated", "Ticket updated"
        BOOKING_CANCELLED = "booking.cancelled", "Booking cancelled by admin"
        ADMIN_PASSWORD_CHANGED = "admin.password_changed", "Administrator changed own password"

    institute = models.ForeignKey(
        Institute,
        on_delete=models.CASCADE,
        related_name="audit_log",
        null=True,
        blank=True,
        help_text="Null for super-administrator actions spanning institutes.",
    )
    actor = models.ForeignKey(
        "laundry.Administrator",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
    )
    actor_label = models.CharField(
        max_length=200,
        help_text="Who acted, as they were named at the time.",
    )
    action = models.CharField(max_length=40, choices=Action.choices, db_index=True)
    target_type = models.CharField(max_length=40, blank=True)
    target_id = models.UUIDField(null=True, blank=True)
    target_label = models.CharField(
        max_length=200,
        blank=True,
        help_text="What was acted on, as it was named at the time.",
    )
    summary = models.CharField(max_length=400)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "admin audit entry"
        verbose_name_plural = "admin audit log"
        indexes = [
            models.Index(fields=("institute", "-created_at")),
            models.Index(fields=("actor", "-created_at")),
        ]

    def __str__(self) -> str:
        return f"{self.actor_label}: {self.summary}"
