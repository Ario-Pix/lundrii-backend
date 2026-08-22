"""Unit tests for Cloudinary ticket photo storage (no live API)."""

from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import APIException

from base.exceptions import CLOUDINARY_NOT_CONFIGURED, CLOUDINARY_UPLOAD_FAILED, S3_NOT_CONFIGURED
from base.storage import ensure_cloudinary_folder, upload_ticket_photo

_CREDENTIALS = {
    "CLOUDINARY_URL": "",
    "CLOUDINARY_CLOUD_NAME": "test-cloud",
    "CLOUDINARY_API_KEY": "test-key",
    "CLOUDINARY_API_SECRET": "test-secret",
    "CLOUDINARY_FOLDER": "lundrii",
}


class CloudinaryErrorCodeTests(SimpleTestCase):
    def test_s3_alias_matches_public_cloudinary_code(self):
        self.assertEqual(CLOUDINARY_NOT_CONFIGURED, "CLOUDINARY_NOT_CONFIGURED")
        self.assertEqual(S3_NOT_CONFIGURED, CLOUDINARY_NOT_CONFIGURED)
        self.assertEqual(CLOUDINARY_UPLOAD_FAILED, "CLOUDINARY_UPLOAD_FAILED")


@override_settings(
    CLOUDINARY_URL="",
    CLOUDINARY_CLOUD_NAME="",
    CLOUDINARY_API_KEY="",
    CLOUDINARY_API_SECRET="",
)
class CloudinaryNotConfiguredTests(SimpleTestCase):
    def test_upload_raises_not_configured(self):
        photo = SimpleUploadedFile("leak.jpg", b"\xff\xd8fake", content_type="image/jpeg")
        with self.assertRaises(APIException) as ctx:
            upload_ticket_photo(photo)
        self.assertEqual(ctx.exception.code, CLOUDINARY_NOT_CONFIGURED)
        self.assertEqual(ctx.exception.status_code, 503)


@override_settings(**_CREDENTIALS)
class CloudinaryUploadTests(SimpleTestCase):
    @patch("cloudinary.uploader.upload")
    def test_upload_uses_folder_and_tickets_public_id(self, mock_upload):
        mock_upload.return_value = {
            "secure_url": (
                "https://res.cloudinary.com/test-cloud/image/upload/v1/lundrii/tickets/abc"
            )
        }
        photo = SimpleUploadedFile(
            "leak.jpg",
            b"\xff\xd8\xff\xe0fakejpeg",
            content_type="image/jpeg",
        )
        url = upload_ticket_photo(photo)
        self.assertIn("/lundrii/", url)
        kwargs = mock_upload.call_args.kwargs
        self.assertEqual(kwargs["folder"], "lundrii")
        self.assertEqual(kwargs["resource_type"], "image")
        self.assertTrue(kwargs["public_id"].startswith("tickets/"))
        self.assertNotIn(".", kwargs["public_id"])

    @patch("cloudinary.uploader.upload", side_effect=RuntimeError("timeout"))
    def test_upload_failure_uses_dedicated_code(self, _mock_upload):
        photo = SimpleUploadedFile("leak.jpg", b"\xff\xd8fake", content_type="image/jpeg")
        with self.assertRaises(APIException) as ctx:
            upload_ticket_photo(photo)
        self.assertEqual(ctx.exception.code, CLOUDINARY_UPLOAD_FAILED)
        self.assertEqual(ctx.exception.status_code, 503)

    @patch("cloudinary.api.create_folder")
    def test_ensure_folder_creates_lundrii(self, mock_create):
        mock_create.return_value = {"success": True}
        self.assertEqual(ensure_cloudinary_folder(), "lundrii")
        mock_create.assert_called_once_with("lundrii")

    @patch("cloudinary.api.create_folder")
    def test_ensure_folder_ignores_already_exists(self, mock_create):
        from cloudinary.exceptions import AlreadyExists

        mock_create.side_effect = AlreadyExists("Folder already exists")
        self.assertEqual(ensure_cloudinary_folder(), "lundrii")
