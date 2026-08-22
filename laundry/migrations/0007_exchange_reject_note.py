from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("laundry", "0006_alter_adminauditlog_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="exchange",
            name="reject_note",
            field=models.TextField(
                blank=True,
                help_text="Optional note from the holder when declining a request.",
            ),
        ),
    ]
