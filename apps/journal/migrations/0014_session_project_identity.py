from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0013_add_session_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="session",
            name="project_identity",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text=(
                    "Stable canonical ID for the project (e.g. git remote URL). "
                    "Authoritative for grouping when present; absent for non-code work."
                ),
                max_length=512,
            ),
        ),
    ]
