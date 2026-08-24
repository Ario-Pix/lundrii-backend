from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from base.email import (
    build_reset_password_link,
    resolve_email_name,
    send_booking_confirmed_email,
    send_login_otp_email,
    send_password_reset_email,
    send_password_reset_email_with_token,
    send_verify_email,
    send_verify_email_with_token,
)
from base.management.commands.send_test_emails import DUMMY_NAME, DUMMY_OTP, DUMMY_TOKEN


def _resend_payload(mock_send):
    mock_send.assert_called_once()
    return mock_send.call_args.args[0]


class ResolveEmailNameTests(SimpleTestCase):
    def test_prefers_explicit_name(self):
        self.assertEqual(
            resolve_email_name(name="  Ada  ", email="ada@gim.ac.in"), "Ada"
        )

    def test_falls_back_to_local_part(self):
        self.assertEqual(resolve_email_name(name="", email="ada@gim.ac.in"), "ada")

    def test_falls_back_to_there(self):
        self.assertEqual(resolve_email_name(name="", email=""), "there")


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
        self.assertTrue(
            send_login_otp_email(to="ada@gim.ac.in", otp="654321", name="Ada Lovelace")
        )
        params = _resend_payload(mock_send)
        self.assertEqual(params["to"], ["ada@gim.ac.in"])
        self.assertEqual(params["subject"], "Ada Lovelace, your Lundrii login code")
        self.assertIn("Hi Ada Lovelace,", params["html"])
        self.assertIn("Hi Ada Lovelace,", params["text"])
        self.assertIn("654321", params["html"])
        self.assertIn("654321", params["text"])
        self.assertIn("#0a1533", params["html"])
        self.assertIn("#0b5fa8", params["html"])
        self.assertIn("#37d392", params["html"])
        self.assertNotIn("<img", params["html"])

    @patch("resend.Emails.send")
    def test_login_otp_falls_back_to_local_part_when_name_missing(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        send_login_otp_email(to="ada@gim.ac.in", otp="654321", name="")
        params = _resend_payload(mock_send)
        self.assertEqual(params["subject"], "ada, your Lundrii login code")
        self.assertIn("Hi ada,", params["html"])
        self.assertIn("Hi ada,", params["text"])

    @patch("resend.Emails.send")
    def test_verify_email_includes_otp_and_frontend_link(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        link = "https://app.lundrii.test/auth/verify?token=abc"
        self.assertTrue(
            send_verify_email(
                to="ada@gim.ac.in",
                otp="111222",
                link=link,
                name="Ada Lovelace",
                email="ada@gim.ac.in",
            )
        )
        params = _resend_payload(mock_send)
        self.assertEqual(params["subject"], "Ada Lovelace, verify your Lundrii email")
        self.assertIn("111222", params["html"])
        self.assertIn("111222", params["text"])
        self.assertIn(link, params["html"])
        self.assertIn(link, params["text"])
        self.assertIn("Hi Ada Lovelace,", params["html"])
        self.assertIn("Hi Ada Lovelace,", params["text"])
        self.assertIn("ada@gim.ac.in", params["html"])
        self.assertIn("ada@gim.ac.in", params["text"])
        self.assertNotIn("Aarav Mehta", params["html"])
        self.assertNotIn("Aarav Mehta", params["text"])

    @patch("resend.Emails.send")
    def test_verify_email_with_token_builds_frontend_url(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        send_verify_email_with_token(
            to="ada@gim.ac.in",
            otp="333444",
            token="tok-v",
            name="Ada Lovelace",
            email="ada@gim.ac.in",
        )
        params = _resend_payload(mock_send)
        expected = "https://app.lundrii.test/auth/verify?token=tok-v"
        self.assertIn(expected, params["html"])
        self.assertIn(expected, params["text"])
        self.assertIn("Hi Ada Lovelace,", params["html"])
        self.assertIn("Hi Ada Lovelace,", params["text"])
        self.assertIn("ada@gim.ac.in", params["html"])
        self.assertNotIn("Aarav Mehta", params["html"])
        self.assertNotIn("Aarav Mehta", params["text"])

    @patch("resend.Emails.send")
    def test_resend_error_payload_is_not_success(self, mock_send):
        mock_send.return_value = {
            "error": {"message": "Domain not verified", "name": "validation_error"}
        }
        with self.assertLogs("base.email", level="ERROR") as logs:
            ok = send_login_otp_email(to="ada@gim.ac.in", otp="111111")
        self.assertFalse(ok)
        self.assertIn("Domain not verified", "\n".join(logs.output))

    @patch("resend.Emails.send")
    def test_resend_logs_response_id_on_success(self, mock_send):
        mock_send.return_value = {"id": "email_abc123"}
        with self.assertLogs("base.email", level="INFO") as logs:
            self.assertTrue(send_login_otp_email(to="ada@gim.ac.in", otp="111111"))
        self.assertIn("email_abc123", "\n".join(logs.output))

    @patch("resend.Emails.send")
    @override_settings(EMAIL_FROM="", RESEND_FROM_EMAIL="")
    def test_resend_missing_from_refuses_noreply_fallback(self, mock_send):
        with self.assertLogs("base.email", level="ERROR") as logs:
            ok = send_login_otp_email(to="ada@gim.ac.in", otp="111111")
        self.assertFalse(ok)
        mock_send.assert_not_called()
        joined = "\n".join(logs.output)
        self.assertIn("noreply@lundrii.app", joined)
        self.assertNotIn("noreply@lundrii.app", str(mock_send.call_args))

    @patch("resend.Emails.send")
    def test_password_reset_includes_otp_and_frontend_link(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        link = "https://app.lundrii.test/auth/reset?token=xyz"
        self.assertTrue(
            send_password_reset_email(
                to="ada@gim.ac.in", otp="777888", link=link, name="Ada Lovelace"
            )
        )
        params = _resend_payload(mock_send)
        self.assertEqual(params["subject"], "Ada Lovelace, reset your Lundrii password")
        self.assertIn("Hi Ada Lovelace,", params["html"])
        self.assertIn("Hi Ada Lovelace,", params["text"])
        self.assertIn("777888", params["html"])
        self.assertIn("777888", params["text"])
        self.assertIn(link, params["html"])
        self.assertIn(link, params["text"])

    @patch("resend.Emails.send")
    def test_password_reset_with_token_builds_frontend_url(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        send_password_reset_email_with_token(
            to="ada@gim.ac.in", otp="999000", token="tok-r", name="Ada Lovelace"
        )
        params = _resend_payload(mock_send)
        expected = "https://app.lundrii.test/auth/reset?token=tok-r"
        self.assertIn(expected, params["html"])
        self.assertIn(expected, params["text"])
        self.assertIn("Hi Ada Lovelace,", params["html"])

    @patch("resend.Emails.send")
    def test_booking_confirmed_includes_name_machine_hostel_when(self, mock_send):
        mock_send.return_value = {"id": "email_test"}
        self.assertTrue(
            send_booking_confirmed_email(
                to="ada@gim.ac.in",
                name="Ada Lovelace",
                machine="3rd Floor · Washer A",
                hostel="Boys Hostel 1",
                when="Mon 24 Aug 2026, 16:00",
            )
        )
        params = _resend_payload(mock_send)
        self.assertEqual(
            params["subject"], "Ada Lovelace, your laundry slot is booked"
        )
        self.assertIn("Hi Ada Lovelace,", params["html"])
        self.assertIn("Hi Ada Lovelace,", params["text"])
        self.assertIn("3rd Floor · Washer A", params["html"])
        self.assertIn("Boys Hostel 1", params["html"])
        self.assertIn("Mon 24 Aug 2026, 16:00", params["html"])
        self.assertIn("3rd Floor · Washer A", params["text"])
        self.assertIn("Boys Hostel 1", params["text"])
        self.assertIn("Mon 24 Aug 2026, 16:00", params["text"])

    @override_settings(ADMIN_FRONTEND_URL="https://admin.lundrii.test")
    def test_admin_password_reset_link_uses_admin_frontend_url(self):
        self.assertEqual(
            build_reset_password_link("tok-admin", is_admin=True),
            "https://admin.lundrii.test/auth/reset?token=tok-admin",
        )


@override_settings(RESEND_API_KEY="")
class SendTestEmailsCommandTests(SimpleTestCase):
    def test_sends_all_templates(self):
        with (
            patch("base.management.commands.send_test_emails.send_login_otp_email") as login,
            patch(
                "base.management.commands.send_test_emails.send_verify_email_with_token"
            ) as verify,
            patch(
                "base.management.commands.send_test_emails.send_password_reset_email_with_token"
            ) as reset,
            patch(
                "base.management.commands.send_test_emails.send_booking_confirmed_email"
            ) as booking,
        ):
            login.return_value = True
            verify.return_value = True
            reset.return_value = True
            booking.return_value = True
            call_command("send_test_emails", "--to", "patharv777@gmail.com")

        login.assert_called_once_with(
            to="patharv777@gmail.com", otp=DUMMY_OTP, name=DUMMY_NAME
        )
        verify.assert_called_once_with(
            to="patharv777@gmail.com",
            otp=DUMMY_OTP,
            token=DUMMY_TOKEN,
            name=DUMMY_NAME,
            email="patharv777@gmail.com",
        )
        reset.assert_called_once_with(
            to="patharv777@gmail.com",
            otp=DUMMY_OTP,
            token=DUMMY_TOKEN,
            name=DUMMY_NAME,
        )
        booking.assert_called_once()
        self.assertEqual(booking.call_args.kwargs["to"], "patharv777@gmail.com")
        self.assertEqual(booking.call_args.kwargs["name"], DUMMY_NAME)

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
            patch(
                "base.management.commands.send_test_emails.send_booking_confirmed_email",
                return_value=True,
            ),
        ):
            with self.assertRaises(CommandError):
                call_command("send_test_emails", "--to", "patharv777@gmail.com")
