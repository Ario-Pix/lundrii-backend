"""Send dummy copies of auth + booking emails (live smoke only)."""

from django.core.management.base import BaseCommand, CommandError

from base.email import (
    send_booking_confirmed_email,
    send_login_otp_email,
    send_password_reset_email_with_token,
    send_verify_email_with_token,
)

DUMMY_OTP = "123456"
DUMMY_TOKEN = "smoke-test-token"
DUMMY_NAME = "Ada Lovelace"


class Command(BaseCommand):
    help = (
        "Send login OTP, verify-email, password-reset, and booking-confirmed "
        "templates to --to. Uses dummy OTP/links. For live Resend smoke, not "
        "the default test suite."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--to",
            required=True,
            help="Recipient address (live smoke uses patharv777@gmail.com).",
        )

    def handle(self, *args, **options):
        to = (options["to"] or "").strip()
        if not to:
            raise CommandError("--to is required")

        sends = (
            (
                "login OTP",
                send_login_otp_email(to=to, otp=DUMMY_OTP, name=DUMMY_NAME),
            ),
            (
                "verify email",
                send_verify_email_with_token(
                    to=to,
                    otp=DUMMY_OTP,
                    token=DUMMY_TOKEN,
                    name=DUMMY_NAME,
                    email=to,
                ),
            ),
            (
                "password reset",
                send_password_reset_email_with_token(
                    to=to, otp=DUMMY_OTP, token=DUMMY_TOKEN, name=DUMMY_NAME
                ),
            ),
            (
                "booking confirmed",
                send_booking_confirmed_email(
                    to=to,
                    name=DUMMY_NAME,
                    machine="3rd Floor · Washer A",
                    hostel="Boys Hostel 1",
                    when="Mon 24 Aug 2026, 16:00",
                ),
            ),
        )

        failed = []
        for label, ok in sends:
            if ok:
                self.stdout.write(self.style.SUCCESS(f"Sent {label} to {to}"))
            else:
                self.stderr.write(self.style.ERROR(f"Failed {label} to {to}"))
                failed.append(label)

        if failed:
            raise CommandError("Failed: " + ", ".join(failed))
