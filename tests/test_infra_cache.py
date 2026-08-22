"""
Cache infrastructure: Django's built-in backends only, no Redis.

OTPs, one-time verify/reset links and rate-limit counters all live in the cache
rather than the database, so the cache is load-bearing for authentication. These
tests pin the two things that could silently break it:

* the auth services behave identically on LocMemCache and on DatabaseCache, so
  moving from a single dev worker to a multi-worker deployment changes nothing;
* nothing in the project imports or configures a Redis client any more.
"""

import time

from django.conf import settings
from django.core.cache import cache, caches
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings

from authentication.services.hashing import hash_secret
from authentication.services.links import (
    consume_reset_link,
    consume_verify_link,
    create_reset_link,
    create_verify_link,
    link_cache_key,
)
from authentication.services.otp import (
    OtpPurpose,
    create_otp,
    otp_cache_key,
    verify_otp,
)

DATABASE_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": settings.CACHE_TABLE_NAME,
        "OPTIONS": {"MAX_ENTRIES": 10000, "CULL_FREQUENCY": 3},
    }
}

LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "lundrii-test-infra",
    }
}


class CacheConfigurationTests(TestCase):
    def test_configured_backend_is_a_django_builtin(self):
        backend = settings.CACHES["default"]["BACKEND"]
        self.assertTrue(
            backend.startswith("django.core.cache.backends."),
            f"Cache backend {backend!r} is not a Django built-in.",
        )

    def test_no_redis_client_is_installed(self):
        # django-redis and redis were dropped; importing either should fail.
        for module_name in ("redis", "django_redis"):
            with self.subTest(module=module_name):
                with self.assertRaises(ImportError):
                    __import__(module_name)

    def test_settings_expose_no_redis_url(self):
        self.assertFalse(hasattr(settings, "REDIS_URL"))


class CacheTableMigrationTests(TestCase):
    def test_cache_table_exists_after_migrations(self):
        """base/0002_cache_table.py must create the DatabaseCache table."""
        self.assertIn(settings.CACHE_TABLE_NAME, connection.introspection.table_names())

    def test_cache_table_has_the_columns_databasecache_needs(self):
        with connection.cursor() as cursor:
            columns = {
                col.name
                for col in connection.introspection.get_table_description(
                    cursor, settings.CACHE_TABLE_NAME
                )
            }
        self.assertEqual(columns, {"cache_key", "value", "expires"})


class CacheBackendBehaviourMixin:
    """Behaviour every configured cache backend must provide."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_set_get_delete_round_trip(self):
        cache.set("lundrii:probe", {"a": 1}, timeout=60)
        self.assertEqual(cache.get("lundrii:probe"), {"a": 1})
        cache.delete("lundrii:probe")
        self.assertIsNone(cache.get("lundrii:probe"))

    def test_add_is_atomic_first_write_wins(self):
        """`cache.add` underpins the OTP rate-limit counter."""
        self.assertTrue(cache.add("lundrii:counter", 1, timeout=60))
        self.assertFalse(cache.add("lundrii:counter", 99, timeout=60))
        self.assertEqual(cache.get("lundrii:counter"), 1)

    def test_incr_counts_up(self):
        cache.add("lundrii:hits", 1, timeout=60)
        self.assertEqual(cache.incr("lundrii:hits"), 2)
        self.assertEqual(cache.incr("lundrii:hits"), 3)

    def test_expiry_removes_the_entry(self):
        cache.set("lundrii:brief", "value", timeout=1)
        self.assertEqual(cache.get("lundrii:brief"), "value")
        time.sleep(1.1)
        self.assertIsNone(cache.get("lundrii:brief"))

    def test_otp_round_trip_stores_only_a_hash(self):
        otp = create_otp("student@gim.ac.in", OtpPurpose.LOGIN)
        stored = cache.get(otp_cache_key("login", "student@gim.ac.in"))
        self.assertEqual(stored, hash_secret(otp))
        self.assertNotEqual(stored, otp)
        self.assertTrue(verify_otp("student@gim.ac.in", otp, OtpPurpose.LOGIN))
        # Single use.
        self.assertFalse(verify_otp("student@gim.ac.in", otp, OtpPurpose.LOGIN))

    def test_one_time_links_round_trip_and_are_single_use(self):
        user_id = "33333333-3333-3333-3333-333333333333"
        verify_token = create_verify_link(user_id)
        reset_token = create_reset_link(user_id)

        # The plaintext token is never a cache key.
        self.assertNotIn(verify_token, link_cache_key("verify", verify_token))

        self.assertEqual(consume_verify_link(verify_token), user_id)
        self.assertIsNone(consume_verify_link(verify_token))
        self.assertEqual(consume_reset_link(reset_token), user_id)
        self.assertIsNone(consume_reset_link(reset_token))


@override_settings(CACHES=LOCMEM_CACHE)
class LocMemCacheTests(CacheBackendBehaviourMixin, TestCase):
    """The dev default: in-process, single worker."""

    def test_backend_in_use(self):
        self.assertEqual(
            caches["default"].__class__.__module__,
            "django.core.cache.backends.locmem",
        )


@override_settings(CACHES=DATABASE_CACHE)
class DatabaseCacheTests(CacheBackendBehaviourMixin, TransactionTestCase):
    """
    The production default: shared across workers, no service beyond the DB.

    TransactionTestCase (not TestCase) because DatabaseCache writes commit, and
    a wrapping test transaction would hide cross-connection behaviour.
    """

    def test_backend_in_use(self):
        self.assertEqual(
            caches["default"].__class__.__module__,
            "django.core.cache.backends.db",
        )

    def test_entries_land_in_the_cache_table(self):
        cache.set("lundrii:persisted", "value", timeout=300)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {settings.CACHE_TABLE_NAME}")
            self.assertEqual(cursor.fetchone()[0], 1)
        self.assertEqual(cache.get("lundrii:persisted"), "value")

    def test_visible_to_a_second_cache_client(self):
        """
        A second client object stands in for a second worker process: with
        DatabaseCache both read the same rows, which is the property LocMemCache
        cannot provide.
        """
        from django.core.cache import caches as other_caches

        cache.set("lundrii:shared", "seen-by-both", timeout=300)
        second_client = other_caches.create_connection("default")
        self.assertEqual(second_client.get("lundrii:shared"), "seen-by-both")
