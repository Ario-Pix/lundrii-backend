"""Request serializers for auth HTTP APIs."""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _validate_password_value(value: str) -> str:
    try:
        validate_password(value)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(list(exc.messages)) from exc
    return value


class RegisterSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32)
    password = serializers.CharField(write_only=True)
    whatsapp_opt_in = serializers.BooleanField(required=False, default=False)
    hostelId = serializers.UUIDField(required=False)
    hostel_id = serializers.UUIDField(required=False)

    def validate_name(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value

    def validate_email(self, value: str) -> str:
        return _normalize_email(value)

    def validate_phone(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Phone is required.")
        return value

    def validate_password(self, value: str) -> str:
        return _validate_password_value(value)

    def validate(self, attrs):
        hostel_id = attrs.get("hostelId") or attrs.get("hostel_id")
        if not hostel_id:
            raise serializers.ValidationError({"hostelId": "Select your hostel."})
        attrs["hostel_id"] = hostel_id
        attrs.pop("hostelId", None)
        return attrs


class SignupHostelOptionSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    instituteId = serializers.UUIDField()
    instituteName = serializers.CharField()


class SignupOptionsResponseSerializer(serializers.Serializer):
    hostels = SignupHostelOptionSerializer(many=True)


class EmailSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value: str) -> str:
        return _normalize_email(value)


class LoginSerializer(serializers.Serializer):
    """Student email + password login."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate_email(self, value: str) -> str:
        return _normalize_email(value)

    def validate_password(self, value: str) -> str:
        value = value or ""
        if not value:
            raise serializers.ValidationError("Password is required.")
        return value


class LoginVerifyOtpSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.CharField()

    def validate_email(self, value: str) -> str:
        return _normalize_email(value)

    def validate_otp(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("OTP is required.")
        return value


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()

    def validate_refresh(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Refresh token is required.")
        return value


class VerifyEmailSerializer(serializers.Serializer):
    token = serializers.CharField(required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True)
    otp = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_email(self, value: str) -> str:
        return _normalize_email(value)

    def validate(self, attrs):
        token = (attrs.get("token") or "").strip()
        email = attrs.get("email") or ""
        otp = (attrs.get("otp") or "").strip()
        if token:
            return {"token": token}
        if email and otp:
            return {"email": email, "otp": otp}
        raise serializers.ValidationError("Provide a token, or email and otp.")


class ResetPasswordSerializer(serializers.Serializer):
    token = serializers.CharField(required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True)
    otp = serializers.CharField(required=False, allow_blank=True, default="")
    password = serializers.CharField(write_only=True)

    def validate_email(self, value: str) -> str:
        return _normalize_email(value)

    def validate_password(self, value: str) -> str:
        return _validate_password_value(value)

    def validate(self, attrs):
        token = (attrs.get("token") or "").strip()
        email = attrs.get("email") or ""
        otp = (attrs.get("otp") or "").strip()
        password = attrs["password"]
        if token:
            return {"token": token, "password": password}
        if email and otp:
            return {"email": email, "otp": otp, "password": password}
        raise serializers.ValidationError(
            "Provide token and password, or email, otp, and password."
        )
