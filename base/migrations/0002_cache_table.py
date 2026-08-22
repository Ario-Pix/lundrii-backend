"""
Create the table backing Django's DatabaseCache.

The cache holds OTPs, one-time verify/reset links and rate-limit counters, so it
has to be shared by every worker process. DatabaseCache gives us that using the
database we already run — no Redis, no separate service. The table lives outside
the migration graph's model state (createcachetable builds it directly), so
`makemigrations` will never try to manage or drop it.
"""

from django.conf import settings
from django.core.management import call_command
from django.db import migrations

TABLE_NAME = getattr(settings, "CACHE_TABLE_NAME", "lundrii_cache")


def create_cache_table(apps, schema_editor):
    # createcachetable is a no-op when the table already exists.
    call_command(
        "createcachetable",
        TABLE_NAME,
        database=schema_editor.connection.alias,
        verbosity=0,
    )


def drop_cache_table(apps, schema_editor):
    connection = schema_editor.connection
    if TABLE_NAME not in connection.introspection.table_names():
        return
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE {connection.ops.quote_name(TABLE_NAME)}")


class Migration(migrations.Migration):

    dependencies = [
        ("base", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, drop_cache_table),
    ]
