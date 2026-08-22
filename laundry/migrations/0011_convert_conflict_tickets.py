from django.db import migrations


def convert_conflict_tickets(apps, schema_editor):
    Ticket = apps.get_model("laundry", "Ticket")
    Ticket.objects.filter(kind="conflict").update(kind="maintenance")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("laundry", "0010_disable_booking_cooldown"),
    ]

    operations = [
        migrations.RunPython(convert_conflict_tickets, noop),
    ]
