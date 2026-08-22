"""Cloudinary helpers for ticket photo uploads."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import unquote, urlparse

from django.conf import settings
from rest_framework import status

from base.exceptions import (
    APIError,
    CLOUDINARY_NOT_CONFIGURED,
    CLOUDINARY_UPLOAD_FAILED,
)


def _cloudinary_folder() -> str:
    folder = getattr(settings, "CLOUDINARY_FOLDER", "") or ""
    return folder.strip() or "lundrii"


def _cloudinary_credentials() -> tuple[str, str, str]:
    """
    Return (cloud_name, api_key, api_secret) from settings.

    Prefer explicit CLOUDINARY_* vars; fall back to parsing CLOUDINARY_URL.
    """
    cloud_name = getattr(settings, "CLOUDINARY_CLOUD_NAME", "") or ""
    api_key = getattr(settings, "CLOUDINARY_API_KEY", "") or ""
    api_secret = getattr(settings, "CLOUDINARY_API_SECRET", "") or ""

    if cloud_name and api_key and api_secret:
        return cloud_name, api_key, api_secret

    url = getattr(settings, "CLOUDINARY_URL", "") or ""
    if not url:
        return "", "", ""

    parsed = urlparse(url)
    if parsed.scheme != "cloudinary" or not parsed.hostname:
        return "", "", ""

    cloud_name = parsed.hostname
    api_key = unquote(parsed.username or "")
    api_secret = unquote(parsed.password or "")
    return cloud_name, api_key, api_secret


def cloudinary_is_configured() -> bool:
    cloud_name, api_key, api_secret = _cloudinary_credentials()
    return bool(cloud_name and api_key and api_secret)


def s3_is_configured() -> bool:
    """Compatibility alias for cloudinary_is_configured."""
    return cloudinary_is_configured()


def _not_configured_error() -> APIError:
    return APIError(
        CLOUDINARY_NOT_CONFIGURED,
        detail=(
            "Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET "
            "(or CLOUDINARY_URL) to upload photos."
        ),
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _configure_cloudinary() -> None:
    """Apply credentials to the Cloudinary SDK. Call before any API use."""
    if not cloudinary_is_configured():
        raise _not_configured_error()

    import cloudinary

    cloud_name, api_key, api_secret = _cloudinary_credentials()
    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )


def ensure_cloudinary_folder() -> str:
    """
    Create CLOUDINARY_FOLDER on the account if it does not already exist.

    Folders are also created on first upload; this explicit call is for smoke
    and first-time setup. Returns the folder name.
    """
    _configure_cloudinary()

    import cloudinary.api
    from cloudinary.exceptions import AlreadyExists, Error

    folder = _cloudinary_folder()
    try:
        cloudinary.api.create_folder(folder)
    except AlreadyExists:
        pass
    except Error as exc:
        message = str(exc).lower()
        if "already exists" not in message and "already exist" not in message:
            raise APIError(
                CLOUDINARY_UPLOAD_FAILED,
                detail=f"Failed to create Cloudinary folder {folder!r}: {exc}",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc
    return folder


def upload_ticket_photo(file: Any) -> str:
    """
    Upload a ticket photo to Cloudinary and return its secure URL.

    Assets land at `{CLOUDINARY_FOLDER}/tickets/{uuid}` (default folder lundrii).
    Raises APIError with CLOUDINARY_NOT_CONFIGURED when env vars are missing,
    or CLOUDINARY_UPLOAD_FAILED on network / API errors.
    """
    _configure_cloudinary()

    import cloudinary.uploader

    public_id = f"tickets/{uuid.uuid4().hex}"
    upload_options: dict[str, Any] = {
        "public_id": public_id,
        "folder": _cloudinary_folder(),
        "resource_type": "image",
    }

    try:
        if hasattr(file, "seek"):
            file.seek(0)
        result = cloudinary.uploader.upload(file, **upload_options)
    except Exception as exc:
        raise APIError(
            CLOUDINARY_UPLOAD_FAILED,
            detail=f"Failed to upload photo to Cloudinary: {exc}",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc

    secure_url = result.get("secure_url") or result.get("url")
    if not secure_url:
        raise APIError(
            CLOUDINARY_UPLOAD_FAILED,
            detail="Cloudinary upload succeeded but returned no URL.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return secure_url
