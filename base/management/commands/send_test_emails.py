"""Send dummy copies of the three auth emails (live smoke only)."""

from django.core.management.base import BaseCommand, CommandError

from base.email import (
    send_login_otp_email,
    send_password_reset_email_with_token,
    send_verify_email_with_token,
)

DUMMY_OTP = "123456"
DUMMY_TOKEN = "smoke-test-token"


class Command(BaseCommand):
    help = (
        "Send login OTP, verify-email, and password-reset templates to --to. "
        "Uses dummy OTP/links. For live Resend smoke, not the default test suite."
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
            ("login OTP", send_login_otp_email(to=to, otp=DUMMY_OTP)),
            (
                "verify email",
                send_verify_email_with_token(to=to, otp=DUMMY_OTP, token=DUMMY_TOKEN),
            ),
            (
                "password reset",
                send_password_reset_email_with_token(
                    to=to, otp=DUMMY_OTP, token=DUMMY_TOKEN
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
