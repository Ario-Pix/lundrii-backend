"""Offline copy_sqlite_to_postgres tests: two SQLite files, no network."""

import sqlite3
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DEFAULT_DB_ALIAS, connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from base.management.commands.copy_sqlite_to_postgres import (
    SOURCE_ALIAS,
    register_sqlite_alias,
    unregister_sqlite_alias,
)
from laundry.models import (
    Booking,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    MachineKind,
    Student,
)
from mcp_server.models import McpToken
from mcp_server.oauth_models import OAuthClient, OAuthRefreshToken

User = get_user_model()


class CopySqliteToPostgresTests(TransactionTestCase):
    # Resolved after we register the temp source file in setUpClass, so the
    # runner never tries to provision a "source" test database (or Neon).
    databases = "__all__"

    @classmethod
    def setUpClass(cls):
        # Snapshot the already-migrated test DB. A second `migrate` would run
        # laundry data migrations against default (wrong schema mid-copy).
        cls._tmp = TemporaryDirectory()
        cls.source_path = Path(cls._tmp.name) / "source.sqlite3"
        default = connections[DEFAULT_DB_ALIAS]
        default.ensure_connection()
        dest = sqlite3.connect(str(cls.source_path))
        try:
            default.connection.backup(dest)
        finally:
            dest.close()
        register_sqlite_alias(SOURCE_ALIAS, cls.source_path)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        unregister_sqlite_alias(SOURCE_ALIAS)
        cls._tmp.cleanup()

    def _run(self, *, force=False, sqlite=None, allow_sqlite=True):
        out = StringIO()
        err = StringIO()
        call_command(
            "copy_sqlite_to_postgres",
            sqlite=str(sqlite or self.source_path),
            force=force,
            allow_sqlite=allow_sqlite,
            stdout=out,
            stderr=err,
            verbosity=0,
        )
        return out.getvalue(), err.getvalue()

    def test_refuses_when_default_is_sqlite(self):
        with self.assertRaises(CommandError) as ctx:
            self._run(allow_sqlite=False)
        self.assertIn("SQLite", str(ctx.exception))

    def test_refuses_when_target_has_users(self):
        User.objects.create_user(email="already@gim.ac.in", password="x")
        with self.assertRaises(CommandError) as ctx:
            self._run()
        self.assertIn("users", str(ctx.exception).lower())

    def test_force_skips_users_guard(self):
        User.objects.create_user(email="already@gim.ac.in", password="x")
        self._run(force=True)
        self.assertTrue(User.objects.filter(email="already@gim.ac.in").exists())

    def test_refuses_same_sqlite_file(self):
        dest = Path(connections[DEFAULT_DB_ALIAS].settings_dict["NAME"]).resolve()
        with self.assertRaises(CommandError) as ctx:
            self._run(sqlite=dest)
        self.assertIn("itself", str(ctx.exception).lower())

    def test_copies_rows_preserves_uuids_and_skips_ephemeral(self):
        now = timezone.now()
        group = Group.objects.using(SOURCE_ALIAS).create(name="committee")
        user = User.objects.db_manager(SOURCE_ALIAS).create_user(
            email="aarav@gim.ac.in",
            password="s3cret",
        )
        user.groups.add(group)
        add_perm = (
            Permission.objects.using(SOURCE_ALIAS)
            .filter(content_type__app_label="laundry", codename="add_institute")
            .select_related("content_type")
            .get()
        )
        user.user_permissions.add(add_perm)

        institute = Institute.objects.using(SOURCE_ALIAS).create(
            name="GIM Copy",
            allowed_email_domains=["gim.ac.in"],
        )
        InstituteRule.objects.using(SOURCE_ALIAS).create(
            institute=institute,
            quota_limit=3,
        )
        hostel = Hostel.objects.using(SOURCE_ALIAS).create(
            institute=institute,
            name="Hostel 1",
        )
        student = Student.objects.using(SOURCE_ALIAS).create(
            user=user,
            institute=institute,
            name="Aarav Mehta",
            home_hostel=hostel,
        )
        machine = Machine.objects.using(SOURCE_ALIAS).create(
            hostel=hostel,
            kind=MachineKind.WASHER,
            location_name="Ground Floor · Washer",
        )
        starts = now + timedelta(days=1)
        booking = Booking.objects.using(SOURCE_ALIAS).create(
            student=student,
            machine=machine,
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
        )

        client = OAuthClient.objects.using(SOURCE_ALIAS).create(
            client_id="lcli_copy_test",
            client_name="Copy client",
            redirect_uris=["https://example.com/cb"],
        )
        refresh_a = OAuthRefreshToken.objects.using(SOURCE_ALIAS).create(
            token_hash="r" * 64,
            client=client,
            student=student,
            expires_at=now + timedelta(days=30),
        )
        refresh_b = OAuthRefreshToken.objects.using(SOURCE_ALIAS).create(
            token_hash="s" * 64,
            client=client,
            student=student,
            expires_at=now + timedelta(days=30),
            rotated_from=refresh_a,
        )
        token = McpToken.objects.using(SOURCE_ALIAS).create(
            student=student,
            oauth_client=client,
            name="Claude",
            token_hash="t" * 64,
            token_hint="lmcp_abc…",
        )

        outstanding = OutstandingToken.objects.using(SOURCE_ALIAS).create(
            user=user,
            jti="copy-test-jti",
            token="refresh-body",
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        BlacklistedToken.objects.using(SOURCE_ALIAS).create(token=outstanding)

        Session.objects.using(SOURCE_ALIAS).create(
            session_key="skip-session-key-not-copied",
            session_data="payload",
            expire_date=now + timedelta(days=1),
        )
        ct = ContentType.objects.using(SOURCE_ALIAS).get(
            app_label="laundry", model="institute"
        )
        LogEntry.objects.using(SOURCE_ALIAS).create(
            user_id=user.pk,
            content_type_id=ct.pk,
            object_id=str(institute.pk),
            object_repr="GIM Copy",
            action_flag=ADDITION,
        )
        with connections[SOURCE_ALIAS].cursor() as cursor:
            cursor.execute(
                "INSERT INTO lundrii_cache (cache_key, value, expires) "
                "VALUES (%s, %s, %s)",
                ["otp:copy-test", "secret", 2147483647],
            )

        src_ids = {
            "user": user.pk,
            "institute": institute.pk,
            "hostel": hostel.pk,
            "student": student.pk,
            "machine": machine.pk,
            "booking": booking.pk,
            "client": client.pk,
            "token": token.pk,
            "refresh_a": refresh_a.pk,
            "refresh_b": refresh_b.pk,
        }

        self._run()

        dest_user = User.objects.get(pk=src_ids["user"])
        self.assertEqual(dest_user.email, "aarav@gim.ac.in")
        self.assertTrue(dest_user.check_password("s3cret"))
        self.assertEqual(
            list(dest_user.groups.values_list("name", flat=True)),
            ["committee"],
        )
        self.assertTrue(dest_user.user_permissions.filter(codename="add_institute").exists())

        dest_institute = Institute.objects.get(pk=src_ids["institute"])
        self.assertEqual(dest_institute.allowed_email_domains, ["gim.ac.in"])
        dest_student = Student.objects.get(pk=src_ids["student"])
        self.assertEqual(dest_student.name, "Aarav Mehta")
        self.assertEqual(dest_student.user_id, src_ids["user"])
        self.assertEqual(dest_student.home_hostel_id, src_ids["hostel"])
        dest_booking = Booking.objects.get(pk=src_ids["booking"])
        self.assertEqual(dest_booking.student_id, src_ids["student"])
        self.assertEqual(dest_booking.machine_id, src_ids["machine"])

        dest_refresh_b = OAuthRefreshToken.objects.get(pk=src_ids["refresh_b"])
        self.assertEqual(dest_refresh_b.rotated_from_id, src_ids["refresh_a"])
        dest_token = McpToken.objects.get(pk=src_ids["token"])
        self.assertEqual(dest_token.oauth_client_id, src_ids["client"])
        self.assertTrue(OutstandingToken.objects.filter(jti="copy-test-jti").exists())

        self.assertFalse(
            Session.objects.filter(session_key="skip-session-key-not-copied").exists()
        )
        self.assertFalse(LogEntry.objects.filter(object_repr="GIM Copy").exists())
        with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM lundrii_cache WHERE cache_key = %s",
                ["otp:copy-test"],
            )
            self.assertEqual(cursor.fetchone()[0], 0)
