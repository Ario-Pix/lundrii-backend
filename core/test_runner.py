"""
Test runner for the Lundrii regression suite.

This suite is meant to be run on *every* backend change, so it has to be fast
and it must not depend on whatever is in the developer's local `.env`. The
runner pins the environment-sensitive settings before any test loads:

* A cheap password hasher. Django 6.1 runs PBKDF2 at 1,500,000 iterations,
  and the suite creates hundreds of users — that alone costs minutes. Password
  *behaviour* is unaffected: hashing, verification and `check_password()` are
  all still exercised, just with a cheaper KDF.
* LocMemCache, so a machine with CACHE_BACKEND=db in `.env` still runs the same
  tests. The DatabaseCache backend is covered explicitly in
  tests/test_infra_cache.py via override_settings.
* The immediate task backend, so enqueued Tasks run inline and can be asserted
  on. tests/test_infra_tasks.py overrides this where it needs the dummy backend.
* No Resend API key, so no test can reach the network.
"""

from django.conf import settings
from django.test.runner import DiscoverRunner


class LundriiTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)

        settings.PASSWORD_HASHERS = [
            "django.contrib.auth.hashers.MD5PasswordHasher",
        ]
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "lundrii-test",
            }
        }
        settings.TASKS = {
            "default": {
                "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
            }
        }
        settings.RESEND_API_KEY = ""
