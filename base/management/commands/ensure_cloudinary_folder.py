"""Create the Cloudinary asset folder (default lundrii) if it is missing."""

from django.core.management.base import BaseCommand, CommandError

from base.exceptions import APIError
from base.storage import cloudinary_is_configured, ensure_cloudinary_folder


class Command(BaseCommand):
    help = "Create CLOUDINARY_FOLDER on Cloudinary if it does not already exist."

    def handle(self, *args, **options):
        if not cloudinary_is_configured():
            raise CommandError(
                "Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, "
                "CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET (or CLOUDINARY_URL)."
            )
        try:
            folder = ensure_cloudinary_folder()
        except APIError as exc:
            raise CommandError(str(exc.detail)) from exc
        self.stdout.write(self.style.SUCCESS(f"Cloudinary folder ready: {folder}"))
