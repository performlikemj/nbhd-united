import logging

from django.db import migrations, models

logger = logging.getLogger(__name__)


def namespace_existing_fingerprints(apps, schema_editor):
    EvidenceEvent = apps.get_model("steward", "EvidenceEvent")
    events = list(EvidenceEvent.objects.all().only("id", "source", "fingerprint").order_by("pk"))
    original_owners = {event.fingerprint: event.pk for event in events}
    final_targets: set[str] = set()
    migrations_by_pk: list[tuple[int, str]] = []

    for event in events:
        target = f"{event.source}:{event.fingerprint}"
        target_owner = original_owners.get(target)
        if (target_owner is not None and target_owner != event.pk) or target in final_targets:
            migrated_target = f"{target}:migrated:{event.pk}"
            logger.warning(
                "Steward fingerprint migration collision event_id=%s target=%s migrated_target=%s",
                event.pk,
                target,
                migrated_target,
            )
            target = migrated_target
        final_targets.add(target)
        migrations_by_pk.append((event.pk, target))

    temporary_targets: set[str] = set()
    occupied = set(original_owners)
    for event in events:
        temporary = f"__steward_0007_migrating__:{event.pk}"
        while temporary in occupied or temporary in temporary_targets:
            temporary = f"{temporary}:x"
        EvidenceEvent.objects.filter(pk=event.pk).update(fingerprint=temporary)
        temporary_targets.add(temporary)

    for event_pk, target in migrations_by_pk:
        EvidenceEvent.objects.filter(pk=event_pk).update(fingerprint=target)


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
        ),
        migrations.AddField(
            model_name="alertstate",
            name="last_reserved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
