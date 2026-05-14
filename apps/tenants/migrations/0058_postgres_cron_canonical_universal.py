"""Flip ``postgres_cron_canonical`` to True for every tenant.

The postgres-canonical flow is now the single architecture for system
cron payload state — all writers go through the postgres CronJob table
and the signal-driven ``regenerate_tenant_crons`` reconciler. The legacy
gateway-canonical paths (force_reseed_crons_task, the inline timezone
resync, etc.) were removed or rewritten in the same change.

Active tenants get their CronJob rows refreshed from the current seed
inside the migration so postgres is the source of truth on first
reconcile after deploy. Suspended tenants are flipped too but skip the
refresh — their containers are hibernated, the reconciler is a no-op
until wake, and at wake time ``refresh_system_cron_rows_from_seed`` runs
via ``update_tenant_config``.

Reversible: the prior flag value isn't preserved (no rollback to
mixed-canonical state — that combination is what this change is
removing).
"""

from __future__ import annotations

import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def flip_and_refresh(apps, schema_editor):
    from django.db import transaction

    Tenant = apps.get_model("tenants", "Tenant")

    Tenant.objects.exclude(postgres_cron_canonical=True).update(postgres_cron_canonical=True)

    # Refresh active tenants' postgres rows from the current seed. Uses the
    # real Tenant model (not the historical one) so build_cron_seed_jobs can
    # access related models / properties as it would at runtime.
    #
    # The whole block is wrapped because querying ``LiveTenant.objects`` emits
    # ``SELECT * FROM tenants`` keyed off the CURRENT model — so when this
    # migration is replayed during a fresh test-DB build that includes later
    # migrations that add Tenant columns, the SELECT references columns not
    # yet present on disk and raises ProgrammingError. The refresh is a
    # nice-to-have (the next apply_pending_configs sweep covers it), so we
    # treat the failure as benign instead of blocking the migration run.
    from apps.orchestrator.services import refresh_system_cron_rows_from_seed
    from apps.tenants.models import Tenant as LiveTenant

    # Savepoint isolates the failure: if the SELECT references columns added
    # by later migrations, we want to roll back JUST this query (not the
    # whole migration's transaction, which would also undo the canonical
    # flag flip above).
    try:
        with transaction.atomic():
            active = list(LiveTenant.objects.filter(status="active").exclude(container_id=""))
    except Exception:
        logger.warning(
            "0058 migration refresh: skipping live-model refresh — "
            "queryset eval failed (likely fresh test DB with later columns "
            "not yet present). The next apply_pending_configs sweep covers it.",
            exc_info=True,
        )
        return

    for tenant in active:
        try:
            result = refresh_system_cron_rows_from_seed(tenant)
            logger.info(
                "0058 migration refresh: tenant %s created=%d updated=%d preserved_custom=%d",
                str(tenant.id)[:8],
                result["created"],
                result["updated"],
                result["preserved_custom"],
            )
        except Exception:
            logger.exception(
                "0058 migration refresh failed for tenant %s (non-fatal — next "
                "apply_pending_configs sweep will catch up)",
                str(tenant.id)[:8],
            )


def noop_reverse(apps, schema_editor):
    """No reversal — the half-state this migration removes was the bug."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0057_tenant_internal_api_key"),
    ]

    operations = [
        migrations.RunPython(flip_and_refresh, noop_reverse),
    ]
