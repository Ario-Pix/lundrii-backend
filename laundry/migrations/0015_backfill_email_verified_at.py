from django.db import migrations
from django.utils import timezone


def backfill_email_verified_at(apps, schema_editor):
    Student = apps.get_model("laundry", "Student")
    Student.objects.filter(email_verified_at__isnull=True).update(
        email_verified_at=timezone.now()
    )


class Migration(migrations.Migration):

    dependencies = [
        ("laundry", "0014_dryer_cap_separate_and_default_on"),
    ]

    operations = [
        migrations.RunPython(backfill_email_verified_at, migrations.RunPython.noop),
    ]
