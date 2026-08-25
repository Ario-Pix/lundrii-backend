"""
Background-task infrastructure: Django's Tasks framework, no Celery.

`base/tasks.py` wraps outbound email in `django.tasks` Tasks. These tests pin
the three properties the rest of the backend relies on:

* the configured backend is a Django built-in and no Celery is installed;
* enqueuing runs the work and reports success/failure on the TaskResult;
* a Task that raises is captured, not propagated — a Resend outage must never
  turn a successful password reset into a 500.
"""

import logging
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.tasks import Task, TaskResultStatus, default_task_backend, task_backends
from django.tasks.backends.dummy import DummyBackend
from django.tasks.backends.immediate import ImmediateBackend
from django.tasks.exceptions import InvalidTask
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from base.tasks import (
    send_booking_confirmed_email_task,
    send_login_otp_email_task,
    send_password_reset_email_task,
)
from laundry.models import Administrator, Hostel, Institute, Machine, MachineKind, Student

ALL_TASKS = (
    send_login_otp_email_task,
    send_password_reset_email_task,
    send_booking_confirmed_email_task,
)

DUMMY_TASKS = {"default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}


class TaskConfigurationTests(SimpleTestCase):
    def test_backend_is_a_django_builtin(self):
        backend = settings.TASKS["default"]["BACKEND"]
        self.assertTrue(
            backend.startswith("django.tasks.backends."),
            f"Task backend {backend!r} is not a Django built-in.",
        )
        self.assertIsInstance(task_backends["default"], ImmediateBackend)

    def test_no_celery_is_installed(self):
        for module_name in ("celery", "kombu", "billiard"):
            with self.subTest(module=module_name):
                with self.assertRaises(ImportError):
                    __import__(module_name)

    def test_every_declared_task_is_a_django_task(self):
        for job in ALL_TASKS:
            with self.subTest(task=job.name):
                self.assertIsInstance(job, Task)
                self.assertEqual(job.backend, "default")
                self.assertEqual(job.queue_name, "default")

    def test_task_functions_are_module_level(self):
        """django.tasks refuses to enqueue anything it cannot import by path."""
        for job in ALL_TASKS:
            with self.subTest(task=job.name):
                job.get_backend().validate_task(job)
                self.assertTrue(job.module_path.startswith("base.tasks."))

    def test_lambdas_are_rejected(self):
        with self.assertRaises(InvalidTask):
            Task(func=lambda: None)


class ImmediateExecutionTests(SimpleTestCase):
    """The configured backend runs enqueued work inline."""

    def test_successful_task_reports_its_return_value(self):
        with patch("base.tasks.send_login_otp_email", return_value=True) as send:
            result = send_login_otp_email_task.enqueue(
                to="committee@gim.ac.in", otp="123456"
            )

        send.assert_called_once_with(to="committee@gim.ac.in", otp="123456", name="")
        self.assertEqual(result.status, TaskResultStatus.SUCCESSFUL)
        self.assertIs(result.is_finished, True)
        self.assertIs(result.return_value, True)
        self.assertEqual(result.errors, [])
        self.assertIsNotNone(result.enqueued_at)
        self.assertIsNotNone(result.finished_at)

    def test_arguments_are_carried_on_the_result(self):
        with patch("base.tasks.send_password_reset_email_with_token", return_value=True):
            result = send_password_reset_email_task.enqueue(
                to="aarav@gim.ac.in",
                otp="654321",
                token="tok-abc",
                name="Aarav Mehta",
            )
        self.assertEqual(result.args, [])
        self.assertEqual(
            result.kwargs,
            {
                "to": "aarav@gim.ac.in",
                "otp": "654321",
                "token": "tok-abc",
                "name": "Aarav Mehta",
            },
        )

    def test_raising_task_is_captured_not_propagated(self):
        with patch(
            "base.tasks.send_password_reset_email_with_token",
            side_effect=RuntimeError("Resend is down"),
        ):
            with self.assertLogs("django.tasks", level="ERROR"):
                result = send_password_reset_email_task.enqueue(
                    to="aarav@gim.ac.in", otp="111111", token="tok-xyz"
                )

        self.assertEqual(result.status, TaskResultStatus.FAILED)
        self.assertEqual(len(result.errors), 1)
        self.assertIs(result.errors[0].exception_class, RuntimeError)
        self.assertIn("Resend is down", result.errors[0].traceback)
        with self.assertRaisesMessage(ValueError, "Task failed"):
            result.return_value


@override_settings(TASKS=DUMMY_TASKS)
class DummyBackendTests(SimpleTestCase):
    """Swapping backends is a settings-only change — no call site moves."""

    def setUp(self):
        default_task_backend.clear()

    def test_enqueue_stores_without_running(self):
        self.assertIsInstance(task_backends["default"], DummyBackend)

        with patch("base.tasks.send_login_otp_email") as send:
            result = send_login_otp_email_task.enqueue(
                to="committee@gim.ac.in", otp="123456"
            )

        send.assert_not_called()
        self.assertEqual(result.status, TaskResultStatus.READY)
        self.assertIs(result.is_finished, False)
        self.assertEqual(len(default_task_backend.results), 1)


class ViewsEnqueueTasksTests(APITestCase):
    """Every outbound email in the API goes through a Task."""

    password = "LundriiTest9!"

    def setUp(self):
        cache.clear()
        self.institute = Institute.objects.create(
            name="Goa Institute of Management",
            allowed_email_domains=["gim.ac.in"],
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

    def _student(self, email="aarav.mehta@gim.ac.in"):
        user = get_user_model().objects.create_user(email=email, password=self.password)
        Student.objects.create(
            user=user,
            institute=self.institute,
            name="Aarav Mehta",
            phone="+91 98220 41127",
        )
        return user

    def _administrator(self, email="committee@gim.ac.in"):
        user = get_user_model().objects.create_user(email=email, password=self.password)
        Administrator.objects.create(
            user=user, institute=self.institute, display_name="Committee"
        )
        return user

    @override_settings(TASKS=DUMMY_TASKS)
    def test_forgot_password_enqueues_a_reset_task(self):
        default_task_backend.clear()
        user = self._student()
        response = self.client.post(
            "/api/v1/auth/forgot-password", {"email": user.email}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = default_task_backend.results
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].task.module_path, send_password_reset_email_task.module_path
        )
        self.assertEqual(results[0].kwargs["name"], "Aarav Mehta")

    @override_settings(TASKS=DUMMY_TASKS)
    def test_admin_otp_request_enqueues_a_login_task(self):
        default_task_backend.clear()
        user = self._administrator()
        response = self.client.post(
            "/api/v1/auth/login/request-otp", {"email": user.email}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = default_task_backend.results
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].task.module_path, send_login_otp_email_task.module_path
        )
        self.assertEqual(results[0].kwargs["name"], "Committee")

    def test_forgot_password_survives_an_email_outage(self):
        """
        The point of routing mail through a Task: the client still gets its
        200 when the mail provider is down.
        """
        user = self._student()
        with patch(
            "base.tasks.send_password_reset_email_with_token",
            side_effect=RuntimeError("Resend is down"),
        ):
            logging.disable(logging.CRITICAL)
            try:
                response = self.client.post(
                    "/api/v1/auth/forgot-password",
                    {"email": user.email},
                    format="json",
                )
            finally:
                logging.disable(logging.NOTSET)

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class EmailFallbackTests(TestCase):
    """With no Resend key configured, email logs to the console and succeeds."""

    @override_settings(RESEND_API_KEY="")
    def test_send_without_api_key_short_circuits_to_logging(self):
        with self.assertLogs("base.email", level="INFO") as logs:
            result = send_login_otp_email_task.enqueue(
                to="committee@gim.ac.in", otp="424242"
            )
        self.assertEqual(result.status, TaskResultStatus.SUCCESSFUL)
        self.assertIs(result.return_value, True)
        self.assertIn("424242", "\n".join(logs.output))
