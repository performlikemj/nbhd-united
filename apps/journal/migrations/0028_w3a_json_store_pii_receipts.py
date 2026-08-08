from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0027_document_family_pii_receipts"),
    ]

    operations = [
        migrations.AddField(
            model_name="dailynote",
            name="pii_receipts",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="journalentry",
            name="pii_receipts",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="pendingextraction",
            name="pii_receipts",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="purpose",
            name="pii_receipts",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="weeklyreview",
            name="pii_receipts",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
