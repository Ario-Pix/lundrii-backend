"""Upload a tiny JPEG to Cloudinary for live smoke tests (not used by manage.py test)."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError

from base.exceptions import APIError
from base.storage import cloudinary_is_configured, upload_ticket_photo

# 1×1 JFIF JPEG used only by this smoke command (not part of manage.py test).
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "080606070605080707070909080a0c140d0c0b0b0c1912130f"
    "141d1a15151d271c1c2020242e2720222c23201c283d2c2c32"
    "323a3e44443a3a464d5555553a485d5d5a5c4f535555ffc000"
    "0b080001000101011100ffc4001f0000010501010101010100"
    "000000000000000102030405060708090a0bffc400b5100002"
    "010303020403050504040000017d0102030004110512213141"
    "061351610722718114328191a1082342b1c11552d1f0243362"
    "7282090a161718191a25262728292a3435363738393a434445"
    "464748494a535455565758595a636465666768696a73747576"
    "7778797a838485868788898a92939495969798999aa2a3a4a5"
    "a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3"
    "d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8"
    "f9faffda0008010100003f00fbd5dbffd9"
)


class Command(BaseCommand):
    help = "Upload a tiny JPEG via upload_ticket_photo and print the secure URL."

    def handle(self, *args, **options):
        if not cloudinary_is_configured():
            raise CommandError(
                "Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, "
                "CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET (or CLOUDINARY_URL)."
            )
        photo = SimpleUploadedFile(
            "smoke.jpg",
            TINY_JPEG,
            content_type="image/jpeg",
        )
        try:
            url = upload_ticket_photo(photo)
        except APIError as exc:
            raise CommandError(str(exc.detail)) from exc
        if "/lundrii/" not in url:
            raise CommandError(
                f"Upload succeeded but URL does not contain /lundrii/: {url}"
            )
        self.stdout.write(self.style.SUCCESS(url))
