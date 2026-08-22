from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from base.email import (
    send_login_otp_email,
    send_password_reset_email,
    send_password_reset_email_with_token,
    send_verify_email,
    send_verify_email_with_token,
)
from base.management.commands.send_test_emails import DUMMY_OTP, DUMMY_TOKEN


def _resend_payload(mock_send):
    mock_send.assert_called_once()
    return mock_send.call_args.args[0]


class ConsoleEmailTests(SimpleTestCase):
    @override_settings(RESEND_API_KEY="")
    def test_login_otp_logs_code_without_resend(self):
        with self.assertLogs("base.email", level="INFO") as logs:
            ok = send_login_otp_email(to="committee@gim.ac.in", otp="424242")
        self.assertTrue(ok)
        self.assertIn("424242", "\n".join(logs.output))


@override_settings(
    RESEND_API_KEY="re_test_not_a_real_key",
    EMAIL_FROM="Lundrii <noreply@lundrii.app>",
    FRONTEND_URL="https://app.lundrii.test",
)
class ResendTemplateTests(SimpleTestCase):
    @patch("resend.Emails.send")
    def test_bare_from_email_is_wrapped_with_display_name(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        with override_settings(EMAIL_FROM="notifications@techconsultancycompany.com"):
            send_login_otp_email(to="ada@gim.ac.in", otp="111111")
        self.assertEqual(
            mock_send.call_args.args[0]["from"],
            "Lundrii <notifications@techconsultancycompany.com>",
        )

    @patch("resend.Emails.send")
    def test_login_otp_subject_and_code_in_html_and_text(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        self.assertTrue(send_login_otp_email(to="ada@gim.ac.in", otp="654321"))
        params = _resend_payload(mock_send)
        self.assertEqual(params["to"], ["ada@gim.ac.in"])
        self.assertEqual(params["subject"], "Your Lundrii login code")
        self.assertIn("654321", params["html"])
        self.assertIn("654321", params["text"])
        self.assertIn("#0a1533", params["html"])
        self.assertIn("#0b5fa8", params["html"])
        self.assertIn("#37d392", params["html"])
        self.assertNotIn("<img", params["html"])

    @patch("resend.Emails.send")
    def test_verify_email_includes_otp_and_frontend_link(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        link = "https://app.lundrii.test/auth/verify?token=abc"
        self.assertTrue(
            send_verify_email(to="ada@gim.ac.in", otp="111222", link=link)
        )
        params = _resend_payload(mock_send)
        self.assertEqual(params["subject"], "Verify your Lundrii email")
        self.assertIn("111222", params["html"])
        self.assertIn("111222", params["text"])
        self.assertIn(link, params["html"])
        self.assertIn(link, params["text"])

    @patch("resend.Emails.send")
    def test_verify_email_with_token_builds_frontend_url(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        send_verify_email_with_token(to="ada@gim.ac.in", otp="333444", token="tok-v")
        params = _resend_payload(mock_send)
        expected = "https://app.lundrii.test/auth/verify?token=tok-v"
        self.assertIn(expected, params["html"])
        self.assertIn(expected, params["text"])

    @patch("resend.Emails.send")
    def test_password_reset_includes_otp_and_frontend_link(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        link = "https://app.lundrii.test/auth/reset?token=xyz"
        self.assertTrue(
            send_password_reset_email(to="ada@gim.ac.in", otp="777888", link=link)
        )
        params = _resend_payload(mock_send)
        self.assertEqual(params["subject"], "Reset your Lundrii password")
        self.assertIn("777888", params["html"])
        self.assertIn("777888", params["text"])
        self.assertIn(link, params["html"])
        self.assertIn(link, params["text"])

    @patch("resend.Emails.send")
    def test_password_reset_with_token_builds_frontend_url(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        send_password_reset_email_with_token(
            to="ada@gim.ac.in", otp="999000", token="tok-r"
        )
        params = _resend_payload(mock_send)
        expected = "https://app.lundrii.test/auth/reset?token=tok-r"
        self.assertIn(expected, params["html"])
        self.assertIn(expected, params["text"])


@override_settings(RESEND_API_KEY="")
class SendTestEmailsCommandTests(SimpleTestCase):
    def test_sends_all_three_templates(self):
        with (
            patch("base.management.commands.send_test_emails.send_login_otp_email") as login,
            patch(
                "base.management.commands.send_test_emails.send_verify_email_with_token"
            ) as verify,
            patch(
                "base.management.commands.send_test_emails.send_password_reset_email_with_token"
            ) as reset,
        ):
            login.return_value = True
            verify.return_value = True
            reset.return_value = True
            call_command("send_test_emails", "--to", "patharv777@gmail.com")

        login.assert_called_once_with(to="patharv777@gmail.com", otp=DUMMY_OTP)
        verify.assert_called_once_with(
            to="patharv777@gmail.com", otp=DUMMY_OTP, token=DUMMY_TOKEN
        )
        reset.assert_called_once_with(
            to="patharv777@gmail.com", otp=DUMMY_OTP, token=DUMMY_TOKEN
        )

    def test_raises_when_a_send_fails(self):
        with (
            patch(
                "base.management.commands.send_test_emails.send_login_otp_email",
                return_value=False,
            ),
            patch(
                "base.management.commands.send_test_emails.send_verify_email_with_token",
                return_value=True,
            ),
            patch(
                "base.management.commands.send_test_emails.send_password_reset_email_with_token",
                return_value=True,
            ),
        ):
            with self.assertRaises(CommandError):
                call_command("send_test_emails", "--to", "patharv777@gmail.com")
