# Track B: Ticket.photo_url

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("laundry", "0001_wave1b_domain_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="photo_url",
            field=models.URLField(blank=True, max_length=500),
        ),
    ]
