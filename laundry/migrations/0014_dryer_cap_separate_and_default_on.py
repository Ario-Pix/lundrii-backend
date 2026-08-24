from django.db import migrations, models


def enable_dryer_cap(apps, schema_editor):
    InstituteRule = apps.get_model("laundry", "InstituteRule")
    InstituteRule.objects.filter(dryer_cap_enabled=False).update(dryer_cap_enabled=True)


class Migration(migrations.Migration):

    dependencies = [
        ("laundry", "0013_remove_hostel_gender"),
    ]

    operations = [
        migrations.AlterField(
            model_name="instituterule",
            name="dryer_cap_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "If enabled, dryer bookings have a separate weekly cap equal to "
                    "quota_limit (same Monday–Sunday window). Dryers never consume "
                    "washer quota."
                ),
            ),
        ),
        migrations.RunPython(enable_dryer_cap, migrations.RunPython.noop),
    ]
