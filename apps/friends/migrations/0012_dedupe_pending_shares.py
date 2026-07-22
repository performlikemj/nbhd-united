from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone


def _dedupe_audience(PendingShare, audience_field, resolved_at):
    audience_id_field = f"{audience_field}_id"
    duplicate_groups = (
        PendingShare.objects.filter(status="pending", **{f"{audience_field}__isnull": False})
        .values("tenant_id", "source_lesson_id", audience_id_field)
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )
    for group in duplicate_groups.iterator():
        lookup = {
            "tenant_id": group["tenant_id"],
            "source_lesson_id": group["source_lesson_id"],
            audience_id_field: group[audience_id_field],
            "status": "pending",
        }
        ordered_ids = list(
            PendingShare.objects.filter(**lookup).order_by("-created_at", "-id").values_list("id", flat=True)
        )
        PendingShare.objects.filter(id__in=ordered_ids[1:]).update(
            status="rejected",
            resolved_at=resolved_at,
        )


def dedupe_pending_shares(apps, schema_editor):
    PendingShare = apps.get_model("friends", "PendingShare")
    resolved_at = timezone.now()
    _dedupe_audience(PendingShare, "target_friendship", resolved_at)
    _dedupe_audience(PendingShare, "target_circle", resolved_at)


class Migration(migrations.Migration):
    dependencies = [
        ("friends", "0011_sky_rls_backstop"),
    ]

    operations = [
        migrations.RunPython(dedupe_pending_shares, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="pendingshare",
            constraint=models.UniqueConstraint(
                fields=("tenant", "source_lesson", "target_friendship"),
                condition=models.Q(status="pending", target_friendship__isnull=False),
                name="uq_pending_share_friendship",
            ),
        ),
        migrations.AddConstraint(
            model_name="pendingshare",
            constraint=models.UniqueConstraint(
                fields=("tenant", "source_lesson", "target_circle"),
                condition=models.Q(status="pending", target_circle__isnull=False),
                name="uq_pending_share_circle",
            ),
        ),
    ]
