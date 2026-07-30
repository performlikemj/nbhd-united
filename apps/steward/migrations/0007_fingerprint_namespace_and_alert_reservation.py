from django.db import migrations, models


def namespace_existing_fingerprints(apps, schema_editor):
    EvidenceEvent = apps.get_model("steward", "EvidenceEvent")
    for event in EvidenceEvent.objects.all().only("id", "source", "fingerprint").iterator():
        event.fingerprint = f"{event.source}:{event.fingerprint}"
        event.save(update_fields=["fingerprint"])


def remove_fingerprint_namespaces(apps, schema_editor):
    EvidenceEvent = apps.get_model("steward", "EvidenceEvent")
    for event in EvidenceEvent.objects.all().only("id", "source", "fingerprint").iterator():
        prefix = f"{event.source}:"
        if event.fingerprint.startswith(prefix):
            event.fingerprint = event.fingerprint.removeprefix(prefix)
            event.save(update_fields=["fingerprint"])


class Migration(migrations.Migration):
    dependencies = [
        ("steward", "0006_relock_after_steward_phase2_hardening"),
    ]

    operations = [
        migrations.AlterField(
            model_name="evidenceevent",
            name="fingerprint",
            field=models.CharField(max_length=192, unique=True),
        ),
        migrations.RunPython(
            namespace_existing_fingerprints,
            remove_fingerprint_namespaces,
        ),
        migrations.AddField(
            model_name="alertstate",
            name="last_reserved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
