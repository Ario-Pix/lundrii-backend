from django.db import migrations


def copy_hostel_gender(apps, schema_editor):
    Student = apps.get_model("laundry", "Student")
    students = (
        Student.objects.filter(gender="")
        .exclude(home_hostel_id=None)
        .select_related("home_hostel")
    )
    for student in students:
        hostel = student.home_hostel
        if hostel is None or not hostel.gender:
            continue
        student.gender = hostel.gender
        student.save(update_fields=["gender", "updated_at"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("laundry", "0008_student_floor"),
    ]

    operations = [
        migrations.RunPython(copy_hostel_gender, noop),
    ]
