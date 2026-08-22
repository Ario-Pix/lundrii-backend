from django.db import migrations, models


def zero_cooldown(apps, schema_editor):
    InstituteRule = apps.get_model("laundry", "InstituteRule")
    InstituteRule.objects.exclude(cooldown_hours=0).update(cooldown_hours=0)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("laundry", "0009_student_gender_from_home_hostel"),
    ]

    operations = [
        migrations.AlterField(
            model_name="instituterule",
            name="cooldown_hours",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Unused. Booking does not require a gap between washes.",
            ),
        ),
        migrations.AlterField(
            model_name="instituterule",
            name="dryer_cap_enabled",
            field=models.BooleanField(
                default=False,
                help_text="If enabled, dryer bookings consume washer quota.",
            ),
        ),
        migrations.RunPython(zero_cooldown, noop),
    ]
