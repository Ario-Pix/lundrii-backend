from unittest.mock import patch

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from authentication.services.hashing import hash_secret
from authentication.services.links import (
    consume_reset_link,
    create_reset_link,
    link_cache_key,
)
from authentication.services.otp import (
    OtpCooldown,
    OtpLocked,
    OtpPurpose,
    OtpRateLimited,
    create_otp,
    delete_otp,
    otp_cache_key,
    record_otp_send,
    verify_otp,
)
from authentication.services.tokens import issue_jwt_pair
from base.email import build_reset_password_link
from laundry.models import Administrator, Gender, Hostel, Institute, Machine, MachineKind, Student, SuperAdministrator


class OtpServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_create_stores_hash_not_plaintext(self):
        otp = create_otp("Ada@School.edu", OtpPurpose.LOGIN)
        self.assertEqual(len(otp), 6)
        self.assertTrue(otp.isdigit())
        stored = cache.get(otp_cache_key("login", "ada@school.edu"))
        self.assertIsNotNone(stored)
        self.assertNotEqual(stored, otp)
        self.assertEqual(stored, hash_secret(otp))

    def test_verify_success_is_single_use(self):
        otp = create_otp("user@example.com", "login")
        self.assertTrue(verify_otp("user@example.com", otp, "login"))
        self.assertFalse(verify_otp("user@example.com", otp, "login"))
        self.assertIsNone(cache.get(otp_cache_key("login", "user@example.com")))

    def test_verify_wrong_code(self):
        create_otp("user@example.com", "login")
        self.assertFalse(verify_otp("user@example.com", "000000", "login"))

    @override_settings(OTP_MAX_ATTEMPTS=3, OTP_COOLDOWN_SECONDS=0)
    def test_lockout_after_max_attempts(self):
        otp = create_otp("user@example.com", "reset", record_send=False)
        for _ in range(2):
            self.assertFalse(verify_otp("user@example.com", "111111", "reset"))
        with self.assertRaises(OtpLocked):
            verify_otp("user@example.com", "111111", "reset")
        with self.assertRaises(OtpLocked):
            verify_otp("user@example.com", otp, "reset")

    @override_settings(OTP_COOLDOWN_SECONDS=60)
    def test_resend_cooldown(self):
        create_otp("user@example.com", "login")
        with self.assertRaises(OtpCooldown):
            create_otp("user@example.com", "login")

    @override_settings(
        OTP_RATE_LIMIT_MAX=2,
        OTP_RATE_LIMIT_WINDOW_SECONDS=900,
        OTP_COOLDOWN_SECONDS=0,
    )
    def test_send_rate_limit(self):
        record_otp_send("user@example.com", "login")
        record_otp_send("user@example.com", "login")
        with self.assertRaises(OtpRateLimited):
            record_otp_send("user@example.com", "login")

    def test_delete_otp(self):
        otp = create_otp("user@example.com", "login")
        delete_otp("user@example.com", "login")
        self.assertFalse(verify_otp("user@example.com", otp, "login"))


class LinkServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_reset_link_round_trip_hashed_key(self):
        user_id = "11111111-1111-1111-1111-111111111111"
        token = create_reset_link(user_id)
        self.assertNotIn(token, link_cache_key("reset", token).split(":")[-1])
        stored = cache.get(link_cache_key("reset", token))
        self.assertEqual(stored, user_id)
        self.assertEqual(consume_reset_link(token), user_id)
        self.assertIsNone(consume_reset_link(token))

    def test_reset_link_unknown_token(self):
        self.assertIsNone(consume_reset_link("not-a-real-token"))

    def test_reset_link_single_use(self):
        token = create_reset_link("22222222-2222-2222-2222-222222222222")
        self.assertIsNotNone(consume_reset_link(token))
        self.assertIsNone(consume_reset_link(token))


class EmailLinkBuilderTests(SimpleTestCase):
    @override_settings(FRONTEND_URL="https://app.lundrii.test")
    def test_frontend_deep_links(self):
        self.assertEqual(
            build_reset_password_link("xyz"),
            "https://app.lundrii.test/auth/reset?token=xyz",
        )


class JwtHelperTests(TestCase):
    def test_issue_jwt_pair(self):
        user = get_user_model().objects.create_user(
            email="student@example.com",
            password="unused-for-otp-login",
        )
        pair = issue_jwt_pair(user)
        self.assertIn("access", pair)
        self.assertIn("refresh", pair)
        access = AccessToken(pair["access"])
        self.assertEqual(str(access["user_id"]), str(user.id))


class AuthAPITests(APITestCase):
    password = "LundriiTest9!"

    def setUp(self):
        cache.clear()
        self.institute = Institute.objects.create(
            name="Goa Institute of Management",
            allowed_email_domains=["gim.ac.in", "@student.gim.ac.in"],
        )
        self.hostel = Hostel.objects.create(
            institute=self.institute, name="Boys Hostel 1"
        )
        Machine.objects.create(
            hostel=self.hostel,
            kind=MachineKind.WASHER,
            location_name="3rd Floor · A Wing",
        )

    def tearDown(self):
        cache.clear()

    def _register_payload(self, email="aarav.mehta@gim.ac.in", **overrides):
        data = {
            "name": "Aarav Mehta",
            "email": email,
            "phone": "+91 98220 41127",
            "password": self.password,
            "whatsapp_opt_in": True,
            "hostelId": str(self.hostel.id),
        }
        data.update(overrides)
        return data

    def _create_student(self, email="aarav.mehta@gim.ac.in", *, verified=False):
        user = get_user_model().objects.create_user(email=email, password=self.password)
        student = Student.objects.create(
            user=user,
            institute=self.institute,
            name="Aarav Mehta",
            phone="+91 98220 41127",
            whatsapp_opt_in=True,
            email_verified_at=timezone.now() if verified else None,
        )
        return user, student

    def _create_administrator(self, email="committee@gim.ac.in"):
        user = get_user_model().objects.create_user(email=email, password=self.password)
        admin = Administrator.objects.create(
            user=user,
            institute=self.institute,
            display_name="Committee",
        )
        return user, admin

    def _create_super_administrator(self, email="super@lundrii.local"):
        user = get_user_model().objects.create_user(email=email, password=self.password)
        profile = SuperAdministrator.objects.create(
            user=user,
            display_name="Platform",
        )
        return user, profile

    def test_register_creates_verified_user(self):
        response = self.client.post(
            "/api/v1/auth/register",
            self._register_payload(floor="3rd Floor"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["email"], "aarav.mehta@gim.ac.in")
        self.assertEqual(response.data["detail"], "Account created.")
        user = get_user_model().objects.get(email="aarav.mehta@gim.ac.in")
        self.assertTrue(user.check_password(self.password))
        student = user.student
        self.assertEqual(student.institute_id, self.institute.id)
        self.assertEqual(student.home_hostel_id, self.hostel.id)
        self.assertFalse(student.floor)
        self.assertEqual(student.gender, "")
        self.assertIsNotNone(student.email_verified_at)

    def test_signup_options_lists_hostels(self):
        response = self.client.get("/api/v1/auth/signup-options")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        hostels = response.data["hostels"]
        self.assertEqual(len(hostels), 1)
        self.assertEqual(hostels[0]["name"], "Boys Hostel 1")
        self.assertNotIn("floors", hostels[0])

    def test_register_requires_hostel(self):
        response = self.client.post(
            "/api/v1/auth/register",
            {
                "name": "Aarav Mehta",
                "email": "new.student@gim.ac.in",
                "phone": "+91 98220 41127",
                "password": self.password,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_domain_rejected(self):
        response = self.client.post(
            "/api/v1/auth/register",
            self._register_payload(email="aarav@gmail.com"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "DOMAIN_REJECTED")
        self.assertIn("gim.ac.in", response.data["allowedDomains"])
        self.assertIn("student.gim.ac.in", response.data["allowedDomains"])
        self.assertFalse(get_user_model().objects.filter(email="aarav@gmail.com").exists())

    def test_register_duplicate_email(self):
        self._create_student()
        response = self.client.post(
            "/api/v1/auth/register",
            self._register_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "ACCOUNT_ALREADY_EXISTS")
        self.assertEqual(response.data["redirectTo"], "login")

    def test_register_cors_preflight_allows_client_platform_header(self):
        response = self.client.options(
            "/api/v1/auth/register",
            HTTP_ORIGIN="http://localhost:3000",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,x-client-platform",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        allow = (response.headers.get("Access-Control-Allow-Headers") or "").lower()
        self.assertIn("x-client-platform", allow)
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"),
            "http://localhost:3000",
        )

    @patch("base.tasks.send_login_otp_email", return_value=True)
    def test_login_request_otp_admin_and_unknown_are_opaque(self, mock_send):
        self._create_administrator()
        known = self.client.post(
            "/api/v1/auth/login/request-otp",
            {"email": "committee@gim.ac.in"},
            format="json",
        )
        unknown_with_password = self.client.post(
            "/api/v1/auth/login/request-otp",
            {"email": "nobody@gim.ac.in", "password": "Any-Pass-9!"},
            format="json",
        )
        self.assertEqual(known.status_code, status.HTTP_200_OK)
        self.assertEqual(unknown_with_password.status_code, status.HTTP_200_OK)
        self.assertEqual(known.data["detail"], unknown_with_password.data["detail"])
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs["to"], "committee@gim.ac.in")

    @patch("base.tasks.send_login_otp_email", return_value=True)
    def test_login_request_otp_unknown_student_email(self, mock_send):
        response = self.client.post(
            "/api/v1/auth/login/request-otp",
            {"email": "nobody@gim.ac.in"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "ACCOUNT_NOT_FOUND")
        self.assertEqual(response.data["redirectTo"], "signup")
        mock_send.assert_not_called()

    @patch("base.tasks.send_login_otp_email", return_value=True)
    def test_login_request_otp_student_sends_email(self, mock_send):
        self._create_student()
        response = self.client.post(
            "/api/v1/auth/login/request-otp",
            {"email": "aarav.mehta@gim.ac.in"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs["to"], "aarav.mehta@gim.ac.in")
        self.assertEqual(mock_send.call_args.kwargs["name"], "Aarav Mehta")

    @patch("base.tasks.send_login_otp_email", return_value=True)
    def test_login_request_otp_admin_with_password_sends_email(self, mock_send):
        user, _ = self._create_administrator()
        response = self.client.post(
            "/api/v1/auth/login/request-otp",
            {"email": user.email, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs["to"], user.email)
        self.assertEqual(mock_send.call_args.kwargs["name"], "Committee")

    @patch("base.tasks.send_login_otp_email", return_value=True)
    def test_login_request_otp_admin_wrong_password_sends_no_email(self, mock_send):
        user, _ = self._create_administrator()
        response = self.client.post(
            "/api/v1/auth/login/request-otp",
            {"email": user.email, "password": "Wrong-Pass-9!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send.assert_not_called()

    @patch("base.tasks.send_login_otp_email", return_value=True)
    def test_login_request_otp_student_with_password_sends_no_email(self, mock_send):
        user, _ = self._create_student()
        response = self.client.post(
            "/api/v1/auth/login/request-otp",
            {"email": user.email, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send.assert_not_called()

    @patch("base.tasks.send_login_otp_email", return_value=True)
    def test_login_verify_otp_issues_jwt_for_admin(self, mock_send):
        user, _ = self._create_administrator()
        self.client.post(
            "/api/v1/auth/login/request-otp",
            {"email": user.email},
            format="json",
        )
        otp = mock_send.call_args.kwargs["otp"]
        response = self.client.post(
            "/api/v1/auth/login/verify-otp",
            {"email": user.email, "otp": otp},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["email"], user.email)
        access = AccessToken(response.data["access"])
        self.assertEqual(str(access["user_id"]), str(user.id))

    @patch("base.tasks.send_login_otp_email", return_value=True)
    def test_login_verify_otp_issues_jwt_for_super_admin(self, mock_send):
        user, _ = self._create_super_administrator()
        self.client.post(
            "/api/v1/auth/login/request-otp",
            {"email": user.email},
            format="json",
        )
        otp = mock_send.call_args.kwargs["otp"]
        response = self.client.post(
            "/api/v1/auth/login/verify-otp",
            {"email": user.email, "otp": otp},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)

    @patch("base.tasks.send_login_otp_email", return_value=True)
    def test_login_verify_otp_issues_jwt_for_student(self, mock_send):
        user, _ = self._create_student(verified=True)
        self.client.post(
            "/api/v1/auth/login/request-otp",
            {"email": user.email},
            format="json",
        )
        otp = mock_send.call_args.kwargs["otp"]
        response = self.client.post(
            "/api/v1/auth/login/verify-otp",
            {"email": user.email, "otp": otp},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["email"], user.email)

    def test_login_verify_wrong_otp(self):
        self._create_administrator()
        create_otp("committee@gim.ac.in", OtpPurpose.LOGIN, record_send=False)
        response = self.client.post(
            "/api/v1/auth/login/verify-otp",
            {"email": "committee@gim.ac.in", "otp": "000000"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "INVALID_OTP")

    def test_password_login_happy_path(self):
        user, _ = self._create_student(verified=True)
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": user.email, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertTrue(response.data["emailVerified"])
        self.assertFalse(response.data["suspended"])
        self.assertEqual(response.data["user"]["email"], user.email)
        self.assertIn("floor", response.data["user"])
        self.assertIn("hostelId", response.data["user"])
        access = AccessToken(response.data["access"])
        self.assertEqual(str(access["user_id"]), str(user.id))

    def test_password_login_unknown_email(self):
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": "nobody@gim.ac.in", "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "ACCOUNT_NOT_FOUND")
        self.assertEqual(response.data["redirectTo"], "signup")

    def test_password_login_wrong_password(self):
        self._create_student(verified=True)
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": "aarav.mehta@gim.ac.in", "password": "Wrong-Pass-9!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["code"], "AUTHENTICATION_FAILED")
        self.assertEqual(response.data["detail"], "Incorrect password.")

    def test_password_login_rejects_admin(self):
        user, _ = self._create_administrator()
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": user.email, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["code"], "AUTHENTICATION_FAILED")

    def test_password_login_rejects_suspended_student(self):
        user, student = self._create_student(verified=True)
        student.suspension_ends = timezone.now() + timedelta(days=3)
        student.suspension_reason = "Missed pickup"
        student.save(update_fields=["suspension_ends", "suspension_reason", "updated_at"])
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": user.email, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["code"], "SUSPENDED")
        self.assertIn("clearsAt", response.data)

    def test_password_login_rejects_disabled_student(self):
        user, student = self._create_student(verified=True)
        student.is_active = False
        student.save(update_fields=["is_active", "updated_at"])
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": user.email, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["code"], "AUTHENTICATION_FAILED")

    def test_password_login_rejects_inactive_user(self):
        user, _ = self._create_student(verified=True)
        user.is_active = False
        user.save(update_fields=["is_active", "updated_at"])
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": user.email, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["code"], "AUTHENTICATION_FAILED")

    @patch("base.tasks.send_password_reset_email_with_token", return_value=True)
    def test_forgot_and_reset_via_token_issues_jwt(self, mock_send):
        user, _ = self._create_student(verified=True)
        forgot = self.client.post(
            "/api/v1/auth/forgot-password",
            {"email": user.email},
            format="json",
        )
        self.assertEqual(forgot.status_code, status.HTTP_200_OK)
        token = mock_send.call_args.kwargs["token"]
        new_password = "N3w-Lundrii-Pass!"
        reset = self.client.post(
            "/api/v1/auth/reset-password",
            {"token": token, "password": new_password},
            format="json",
        )
        self.assertEqual(reset.status_code, status.HTTP_200_OK)
        self.assertIn("access", reset.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password(new_password))

    @patch("base.tasks.send_password_reset_email_with_token", return_value=True)
    def test_forgot_unknown_email_is_opaque(self, mock_send):
        response = self.client.post(
            "/api/v1/auth/forgot-password",
            {"email": "ghost@gim.ac.in"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send.assert_not_called()

    @override_settings(ADMIN_FRONTEND_URL="https://admin.lundrii.test")
    @patch("authentication.views.send_password_reset_email_with_token", return_value=True)
    def test_forgot_admin_uses_admin_frontend_reset_link(self, mock_send):
        user, _ = self._create_administrator()
        response = self.client.post(
            "/api/v1/auth/forgot-password",
            {"email": user.email},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_send.assert_called_once()
        self.assertTrue(mock_send.call_args.kwargs["is_admin"])

    @patch("base.tasks.send_password_reset_email_with_token", return_value=True)
    def test_reset_via_otp(self, mock_send):
        user, _ = self._create_student()
        self.client.post(
            "/api/v1/auth/forgot-password",
            {"email": user.email},
            format="json",
        )
        otp = mock_send.call_args.kwargs["otp"]
        response = self.client.post(
            "/api/v1/auth/reset-password",
            {
                "email": user.email,
                "otp": otp,
                "password": "An0ther-Strong-Pass!",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.check_password("An0ther-Strong-Pass!"))

    def test_refresh_and_logout_blacklist(self):
        user, _ = self._create_student(verified=True)
        login = self.client.post(
            "/api/v1/auth/login",
            {"email": user.email, "password": self.password},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        refresh = login.data["refresh"]

        refreshed = self.client.post(
            "/api/v1/auth/refresh",
            {"refresh": refresh},
            format="json",
        )
        self.assertEqual(refreshed.status_code, status.HTTP_200_OK)
        self.assertIn("access", refreshed.data)

        logout = self.client.post(
            "/api/v1/auth/logout",
            {"refresh": refresh},
            format="json",
        )
        self.assertEqual(logout.status_code, status.HTTP_204_NO_CONTENT)

        after_logout = self.client.post(
            "/api/v1/auth/refresh",
            {"refresh": refresh},
            format="json",
        )
        self.assertEqual(after_logout.status_code, status.HTTP_401_UNAUTHORIZED)
