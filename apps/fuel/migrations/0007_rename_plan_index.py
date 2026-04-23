from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("fuel", "0006_workout_plans"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="workoutplan",
            new_name="fuel_workou_tenant__1ae46c_idx",
            old_name="fuel_workout_tenant_status_idx",
        ),
    ]
