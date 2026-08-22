"""
Custom DRF exception handler with stable API error codes.
"""

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler


# Stable error codes used across auth and laundry APIs
DOMAIN_REJECTED = "DOMAIN_REJECTED"
RULE_BLOCKED = "RULE_BLOCKED"
SLOT_TAKEN = "SLOT_TAKEN"
UNVERIFIED = "UNVERIFIED"
SUSPENDED = "SUSPENDED"
MACHINE_OFFLINE = "MACHINE_OFFLINE"
OUTSIDE_ADVANCE_WINDOW = "OUTSIDE_ADVANCE_WINDOW"
PAST_SLOT = "PAST_SLOT"
INVALID_OTP = "INVALID_OTP"
RATE_LIMITED = "RATE_LIMITED"
NOT_FOUND = "NOT_FOUND"
VALIDATION_ERROR = "VALIDATION_ERROR"
AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
PERMISSION_DENIED = "PERMISSION_DENIED"
CLOUDINARY_NOT_CONFIGURED = "CLOUDINARY_NOT_CONFIGURED"
S3_NOT_CONFIGURED = CLOUDINARY_NOT_CONFIGURED  # alias; public API uses CLOUDINARY_NOT_CONFIGURED
CLOUDINARY_UPLOAD_FAILED = "CLOUDINARY_UPLOAD_FAILED"


class APIError(APIException):
    """Raise with a stable `code` for clients."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Request failed."
    default_code = "error"

    def __init__(self, code, detail=None, status_code=None, extra=None):
        self.code = code
        self.extra = extra or {}
        if status_code is not None:
            self.status_code = status_code
        super().__init__(detail=detail or self.default_detail, code=code)


def custom_exception_handler(exc, context):
    """
    Normalize errors to:
    { "code": "...", "detail": "...", ...optional extra fields }
    """
    response = exception_handler(exc, context)

    if response is None:
        return None

    if isinstance(exc, APIError):
        raw = (
            response.data.get("detail")
            if isinstance(response.data, dict)
            else response.data
        )
        payload = {
            "code": exc.code,
            "detail": _detail_as_str(raw),
        }
        payload.update(exc.extra)
        response.data = payload
        return response

    # Map common DRF exceptions to stable codes
    code = getattr(exc, "default_code", None) or "error"
    code_map = {
        "not_authenticated": AUTHENTICATION_FAILED,
        "authentication_failed": AUTHENTICATION_FAILED,
        "permission_denied": PERMISSION_DENIED,
        "not_found": NOT_FOUND,
        "invalid": VALIDATION_ERROR,
    }
    mapped = code_map.get(str(code), str(code).upper() if isinstance(code, str) else "ERROR")

    if isinstance(response.data, dict) and "detail" in response.data:
        response.data = {
            "code": mapped,
            "detail": _detail_as_str(response.data.get("detail")),
        }
    elif isinstance(response.data, dict):
        # Validation errors (field-keyed)
        response.data = {
            "code": VALIDATION_ERROR,
            "detail": "Validation failed.",
            "errors": response.data,
        }
    else:
        response.data = {
            "code": mapped,
            "detail": _detail_as_str(response.data),
        }

    return response


def _detail_as_str(detail) -> str:
    if detail is None:
        return "An error occurred."
    if isinstance(detail, list):
        return "; ".join(str(item) for item in detail)
    return str(detail)
