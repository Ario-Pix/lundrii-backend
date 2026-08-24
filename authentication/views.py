"""Auth HTTP APIs: password login, OTP login, verify email, forgot/reset."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from authentication.serializers import (
    EmailSerializer,
    LoginSerializer,
    LoginRequestOtpSerializer,
    LoginVerifyOtpSerializer,
    LogoutSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    SignupOptionsResponseSerializer,
    VerifyEmailSerializer,
)
from authentication.services import (
    OtpCooldown,
    OtpLocked,
    OtpPurpose,
    OtpRateLimited,
    create_otp,
    create_reset_link,
    create_verify_link,
    consume_reset_link,
    consume_verify_link,
    issue_jwt_pair,
    record_otp_send,
    verify_otp,
)
from authentication.services.institutes import (
    collect_allowed_domains,
    resolve_institute_for_email,
)
from base.clients import declared_channel
from base.email import send_password_reset_email_with_token
from base.tasks import (
    send_login_otp_email_task,
    send_password_reset_email_task,
    send_verify_email_task,
)
from base.exceptions import (
    AUTHENTICATION_FAILED,
    DOMAIN_REJECTED,
    INVALID_OTP,
    NOT_FOUND,
    RATE_LIMITED,
    SUSPENDED,
    VALIDATION_ERROR,
    APIError,
)
from base.permissions import user_is_administrator, user_is_super_administrator
from laundry.models import Hostel, Student
from laundry.services.rules import student_gender

User = get_user_model()

OPAQUE_LOGIN = "If an account exists for this email, a login code has been sent."
OPAQUE_FORGOT = (
    "If an account exists for this email, password reset instructions have been sent."
)
OPAQUE_RESEND = (
    "If an unverified account exists for this email, a verification email has been sent."
)
OPAQUE_CREDENTIALS = "Invalid email or password."


def _get_student(user) -> Student | None:
    try:
        return user.student
    except (ObjectDoesNotExist, AttributeError):
        return None


def _display_name_for_email(user) -> str:
    """Best-effort display name for outbound mail; empty if unknown."""
    student = _get_student(user)
    if student is not None:
        name = (student.name or "").strip()
        if name:
            return name
    for attr in ("administrator", "superadministrator"):
        try:
            profile = getattr(user, attr)
        except (ObjectDoesNotExist, AttributeError):
            continue
        if profile is None:
            continue
        name = (getattr(profile, "display_name", None) or "").strip()
        if name:
            return name
    return ""


def _is_admin_user(user) -> bool:
    """Administrator or SuperAdministrator with an active profile."""
    return user_is_administrator(user) or user_is_super_administrator(user)


def _can_login_with_otp(user) -> bool:
    """Students and committee admins may sign in with a one-time email code."""
    return _get_student(user) is not None or _is_admin_user(user)


def _flags(user) -> tuple[bool, bool]:
    student = _get_student(user)
    if student is None:
        return True, False
    return student.is_email_verified, student.is_suspended


def user_summary(user) -> dict:
    student = _get_student(user)
    email_verified, suspended = _flags(user)
    payload = {
        "id": str(user.id),
        "email": user.email,
        "is_active": user.is_active,
        "emailVerified": email_verified,
        "suspended": suspended,
    }
    if student is None:
        return payload
    payload.update(
        {
            "name": student.name,
            "phone": student.phone,
            "whatsapp_opt_in": student.whatsapp_opt_in,
            "gender": student_gender(student) or None,
            "institute_id": str(student.institute_id),
            "home_hostel_id": (
                str(student.home_hostel_id) if student.home_hostel_id else None
            ),
            "hostelId": str(student.home_hostel_id) if student.home_hostel_id else None,
            "hostelName": student.home_hostel.name if student.home_hostel else None,
            "floor": student.floor or None,
            "email_verified_at": (
                student.email_verified_at.isoformat() if student.email_verified_at else None
            ),
            "suspension_ends": (
                student.suspension_ends.isoformat() if student.suspension_ends else None
            ),
            "suspension_reason": student.suspension_reason or None,
        }
    )
    return payload


def auth_token_payload(user, request=None) -> dict:
    """
    The standard login response.

    ``request`` is used only to work out which app is logging in, so the client
    can be stamped into the JWT and every later booking records where it came
    from. See ``base.clients``.
    """
    email_verified, suspended = _flags(user)
    return {
        **issue_jwt_pair(
            user, client=declared_channel(request) if request is not None else None
        ),
        "emailVerified": email_verified,
        "suspended": suspended,
        "user": user_summary(user),
    }


def raise_for_otp_service(exc: Exception) -> None:
    if isinstance(exc, (OtpCooldown, OtpRateLimited)):
        raise APIError(
            RATE_LIMITED,
            detail="Too many requests. Try again later.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            extra={"retry_after": exc.retry_after},
        ) from exc
    if isinstance(exc, OtpLocked):
        raise APIError(
            INVALID_OTP,
            detail="Too many failed attempts. Request a new code.",
            extra={"retry_after": exc.retry_after},
        ) from exc
    raise exc


def record_send_or_rate_limit(email: str, purpose: OtpPurpose) -> None:
    try:
        record_otp_send(email, purpose)
    except (OtpCooldown, OtpRateLimited, OtpLocked) as exc:
        raise_for_otp_service(exc)


def verify_otp_or_invalid(email: str, otp: str, purpose: OtpPurpose) -> None:
    try:
        ok = verify_otp(email, otp, purpose)
    except (OtpCooldown, OtpRateLimited, OtpLocked) as exc:
        raise_for_otp_service(exc)
    if not ok:
        raise APIError(INVALID_OTP, detail="Invalid or expired code.")


def _active_user_by_email(email: str):
    return User.objects.filter(email__iexact=email, is_active=True).first()


class SignupOptionsView(APIView):
    """Public hostel list for the sign-up dropdown."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = SignupOptionsResponseSerializer

    def get(self, request):
        email = (request.query_params.get("email") or "").strip().lower()
        hostels = Hostel.objects.filter(
            is_active=True, institute__is_active=True
        ).select_related("institute")
        if email:
            institute = resolve_institute_for_email(email)
            hostels = hostels.filter(institute=institute) if institute else hostels.none()
        payload = []
        for hostel in hostels.order_by("institute__name", "name"):
            payload.append(
                {
                    "id": str(hostel.id),
                    "name": hostel.name,
                    "instituteId": str(hostel.institute_id),
                    "instituteName": hostel.institute.name,
                }
            )
        return Response({"hostels": payload})


class RegisterView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = RegisterSerializer

    def post(self, request):
        ser = RegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        email = data["email"]

        institute = resolve_institute_for_email(email)
        if institute is None:
            raise APIError(
                DOMAIN_REJECTED,
                detail="This email domain is not on any institute allow-list.",
                extra={"allowedDomains": collect_allowed_domains()},
            )

        if User.objects.filter(email__iexact=email).exists():
            raise APIError(
                VALIDATION_ERROR,
                detail="An account with this email already exists.",
            )

        try:
            hostel = Hostel.objects.select_related("institute").get(
                pk=data["hostel_id"], is_active=True
            )
        except (Hostel.DoesNotExist, ValueError, TypeError) as exc:
            raise APIError(
                VALIDATION_ERROR,
                detail="Select a valid hostel.",
                extra={"hostelId": ["Unknown hostel."]},
            ) from exc

        if hostel.institute_id != institute.id:
            raise APIError(
                VALIDATION_ERROR,
                detail="That hostel is not part of your institute.",
                extra={"hostelId": ["Hostel is outside your institute."]},
            )

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=email,
                    password=data["password"],
                )
                student = Student.objects.create(
                    user=user,
                    institute=institute,
                    name=data["name"],
                    phone=data["phone"],
                    whatsapp_opt_in=data["whatsapp_opt_in"],
                    home_hostel=hostel,
                    gender="",
                )
        except IntegrityError as exc:
            raise APIError(
                VALIDATION_ERROR,
                detail="An account with this email already exists.",
            ) from exc

        try:
            otp = create_otp(email, OtpPurpose.VERIFY)
        except (OtpCooldown, OtpRateLimited, OtpLocked) as exc:
            raise_for_otp_service(exc)
        token = create_verify_link(user.id)
        send_verify_email_task.enqueue(
            to=user.email,
            otp=otp,
            token=token,
            name=student.name,
            email=user.email,
        )

        return Response(
            {
                "detail": "Verification email sent.",
                "email": user.email,
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    """Student email + password → JWT. Admins must use OTP login."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = LoginSerializer

    def post(self, request):
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"]
        password = ser.validated_data["password"]

        user = User.objects.filter(email__iexact=email).first()
        if user is None or not user.check_password(password):
            raise APIError(
                AUTHENTICATION_FAILED,
                detail=OPAQUE_CREDENTIALS,
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        if not user.is_active:
            raise APIError(
                AUTHENTICATION_FAILED,
                detail=OPAQUE_CREDENTIALS,
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        student = _get_student(user)
        if student is None or not student.is_active:
            raise APIError(
                AUTHENTICATION_FAILED,
                detail=OPAQUE_CREDENTIALS,
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        if student.is_suspended:
            raise APIError(
                SUSPENDED,
                detail=(
                    "You cannot sign in while your account is suspended."
                ),
                status_code=status.HTTP_403_FORBIDDEN,
                extra={
                    "clearsAt": (
                        student.suspension_ends.isoformat()
                        if student.suspension_ends
                        else None
                    )
                },
            )

        return Response(auth_token_payload(user, request))


class LoginRequestOtpView(APIView):
    """OTP request for students and admins. Unknown emails get the same opaque success."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = LoginRequestOtpSerializer

    def post(self, request):
        ser = LoginRequestOtpSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"]
        password = ser.validated_data.get("password")

        record_send_or_rate_limit(email, OtpPurpose.LOGIN)

        user = _active_user_by_email(email)
        should_send = False
        if password is None:
            # Existing student email-only OTP flow (and legacy admin behavior).
            should_send = user is not None and _can_login_with_otp(user)
        else:
            # Admin portal flow: password is required and must match an admin user.
            should_send = (
                user is not None
                and _is_admin_user(user)
                and bool(password)
                and user.check_password(password)
            )

        if should_send:
            otp = create_otp(email, OtpPurpose.LOGIN, record_send=False)
            send_login_otp_email_task.enqueue(
                to=user.email,
                otp=otp,
                name=_display_name_for_email(user),
            )

        return Response({"detail": OPAQUE_LOGIN})


class LoginVerifyOtpView(APIView):
    """OTP verify → JWT for students and admins. Unknown emails get INVALID_OTP."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = LoginVerifyOtpSerializer

    def post(self, request):
        ser = LoginVerifyOtpSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"]
        otp = ser.validated_data["otp"]

        user = _active_user_by_email(email)
        if user is None or not _can_login_with_otp(user):
            # Still consume a cached OTP if present so lockout/rate apply equally.
            try:
                verify_otp(email, otp, OtpPurpose.LOGIN)
            except (OtpCooldown, OtpRateLimited, OtpLocked) as exc:
                raise_for_otp_service(exc)
            raise APIError(INVALID_OTP, detail="Invalid or expired code.")

        verify_otp_or_invalid(email, otp, OtpPurpose.LOGIN)
        return Response(auth_token_payload(user, request))


class LogoutView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = LogoutSerializer

    def post(self, request):
        ser = LogoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        raw = ser.validated_data["refresh"]
        try:
            RefreshToken(raw).blacklist()
        except Exception:
            # Invalid/expired/already-blacklisted, or blacklist app unavailable.
            pass
        return Response(status=status.HTTP_204_NO_CONTENT)


class RefreshView(TokenRefreshView):
    permission_classes = [AllowAny]
    authentication_classes: list = []


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = VerifyEmailSerializer

    def post(self, request):
        ser = VerifyEmailSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        if data.get("token"):
            user_id = consume_verify_link(data["token"])
            if not user_id:
                raise APIError(INVALID_OTP, detail="Invalid or expired verification link.")
            user = User.objects.filter(pk=user_id, is_active=True).first()
            if user is None:
                raise APIError(INVALID_OTP, detail="Invalid or expired verification link.")
        else:
            verify_otp_or_invalid(data["email"], data["otp"], OtpPurpose.VERIFY)
            user = _active_user_by_email(data["email"])
            if user is None:
                raise APIError(INVALID_OTP, detail="Invalid or expired code.")

        student = _get_student(user)
        if student is None:
            raise APIError(
                NOT_FOUND,
                detail="No student account found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        if student.email_verified_at is None:
            student.email_verified_at = timezone.now()
            student.save(update_fields=["email_verified_at", "updated_at"])

        return Response(
            {
                "detail": "Email verified.",
                "emailVerified": True,
                "email": user.email,
            }
        )


class ResendVerificationView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = EmailSerializer

    def post(self, request):
        ser = EmailSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"]

        record_send_or_rate_limit(email, OtpPurpose.VERIFY)

        user = _active_user_by_email(email)
        student = _get_student(user) if user is not None else None
        if student is not None and student.email_verified_at is None:
            otp = create_otp(email, OtpPurpose.VERIFY, record_send=False)
            token = create_verify_link(user.id)
            send_verify_email_task.enqueue(
                to=user.email,
                otp=otp,
                token=token,
                name=student.name,
                email=user.email,
            )

        return Response({"detail": OPAQUE_RESEND})


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = EmailSerializer

    def post(self, request):
        ser = EmailSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"]

        record_send_or_rate_limit(email, OtpPurpose.RESET)

        user = _active_user_by_email(email)
        if user is not None:
            otp = create_otp(email, OtpPurpose.RESET, record_send=False)
            token = create_reset_link(user.id)
            name = _display_name_for_email(user)
            if _is_admin_user(user):
                send_password_reset_email_with_token(
                    to=user.email,
                    otp=otp,
                    token=token,
                    is_admin=True,
                    name=name,
                )
            else:
                send_password_reset_email_task.enqueue(
                    to=user.email, otp=otp, token=token, name=name
                )

        return Response({"detail": OPAQUE_FORGOT})


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = ResetPasswordSerializer

    def post(self, request):
        ser = ResetPasswordSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        if data.get("token"):
            user_id = consume_reset_link(data["token"])
            if not user_id:
                raise APIError(INVALID_OTP, detail="Invalid or expired reset link.")
            user = User.objects.filter(pk=user_id, is_active=True).first()
            if user is None:
                raise APIError(INVALID_OTP, detail="Invalid or expired reset link.")
        else:
            verify_otp_or_invalid(data["email"], data["otp"], OtpPurpose.RESET)
            user = _active_user_by_email(data["email"])
            if user is None:
                raise APIError(INVALID_OTP, detail="Invalid or expired code.")

        user.set_password(data["password"])
        user.save(update_fields=["password", "updated_at"])
        return Response(auth_token_payload(user, request))
