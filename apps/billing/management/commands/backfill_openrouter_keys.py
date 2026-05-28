"""Backfill per-tenant OpenRouter sub-keys for existing tenants (PR #1.6 Phase 5).

New tenants get a per-tenant OR sub-key during ``provision_tenant`` when
``OPENROUTER_PER_TENANT_KEYS_ENABLED`` is True. This command handles
tenants that existed before the flag flipped, or where the original
provisioning fell back to the shared key because OR was down.

Workflow per tenant:

  1. Create OR sub-key via ``create_sub_key`` (label, spend limit from tier).
  2. Write the returned key string to KV at ``<key_vault_prefix>-openrouter-key``.
  3. Persist ``openrouter_key_secret_name`` + ``openrouter_key_hash`` on the
     Tenant row.
  4. Rebind the container's ``openrouter-key`` secret to the per-tenant KV
     entry via ``update_container_openrouter_key_secret``. This forces a
     new revision so Container Apps re-fetches the KV value (a plain
     restart would keep the cached old binding).
  5. Bump ``pending_config_version`` for completeness.

Idempotent. A tenant that already has a non-empty ``openrouter_key_secret_name``
is skipped. Safe to re-run after fixing transient failures.

Usage:

    python manage.py backfill_openrouter_keys              # all tenants
    python manage.py backfill_openrouter_keys --tenant <uuid>   # one
    python manage.py backfill_openrouter_keys --dry-run    # report only
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def _eligible_tenants(tenant_id: str | None) -> list[Tenant]:
    qs = Tenant.objects.filter(
        status__in=[Tenant.Status.ACTIVE, Tenant.Status.SUSPENDED],
        openrouter_key_secret_name="",
    )
    if tenant_id:
        qs = qs.filter(id=tenant_id)
    return list(qs.select_related("user"))


def _backfill_one(tenant: Tenant, dry_run: bool, stdout) -> bool:
    """Create + persist + rebind a sub-key for one tenant. Returns True on
    success, False on any failure (already logged)."""
    from apps.billing.constants import TIER_COST_BUDGETS
    from apps.billing.openrouter_admin import (
        OpenRouterAdminError,
        create_sub_key,
        secret_name_for_tenant,
    )
    from apps.byo_models.services import _write_secret_to_kv
    from apps.orchestrator.azure_client import (
        assign_key_vault_role,
        get_identity_client,
        update_container_openrouter_key_secret,
    )

    tid = str(tenant.id)[:8]
    tier = tenant.model_tier or "starter"
    limit = float(TIER_COST_BUDGETS.get(tier, 5.00))
    label = f"tenant-{tid}"

    if dry_run:
        stdout.write(f"[DRY-RUN] would create sub-key for tenant={tid} label={label} limit=${limit:.2f}/mo")
        return True

    try:
        api_key, key_hash = create_sub_key(label, limit_dollars=limit, limit_reset="monthly")
    except OpenRouterAdminError as exc:
        stdout.write(f"[FAIL] tenant={tid} create_sub_key: {exc}")
        return False

    try:
        secret_name = secret_name_for_tenant(tenant)
    except OpenRouterAdminError as exc:
        stdout.write(f"[FAIL] tenant={tid} secret_name lookup: {exc}")
        return False

    try:
        _write_secret_to_kv(secret_name, api_key)
    except Exception as exc:
        # Leaves an orphan sub-key on OR — caught by the sweeper.
        stdout.write(f"[FAIL] tenant={tid} KV write to {secret_name}: {exc}")
        logger.warning("Orphan OR sub-key hash=%s for tenant=%s (KV write failed)", key_hash, tid)
        return False

    # Grant the tenant's managed identity read access on the brand-new KV
    # secret. ``provision_tenant`` bundles this into its single
    # ``assign_key_vault_role`` call; the backfill has to do it explicitly,
    # otherwise the rebind below fails with "Unable to get value using
    # Managed identity ... for secret openrouter-key" — the secret-set
    # API resolves the KV ref synchronously, so the container's MI must
    # already be authorized.
    if tenant.managed_identity_id:
        try:
            mi_name = tenant.managed_identity_id.rsplit("/", 1)[-1]
            mi = get_identity_client().user_assigned_identities.get(
                resource_group_name=settings.AZURE_RESOURCE_GROUP,
                resource_name=mi_name,
            )
            assign_key_vault_role(mi.principal_id, secret_names=[secret_name])
        except Exception as exc:
            stdout.write(f"[FAIL] tenant={tid} KV role grant for {secret_name}: {exc}")
            logger.warning("KV role grant failed for tenant=%s secret=%s", tid, secret_name)
            return False

    tenant.openrouter_key_secret_name = secret_name
    tenant.openrouter_key_hash = key_hash
    tenant.pending_config_version = (tenant.pending_config_version or 0) + 1
    tenant.save(
        update_fields=[
            "openrouter_key_secret_name",
            "openrouter_key_hash",
            "pending_config_version",
            "updated_at",
        ]
    )

    # Rebind container env var — only for ACTIVE tenants with a live
    # container. SUSPENDED tenants' containers are deactivated and will
    # pick up the new KV binding on the next reactivation revision.
    if tenant.status == Tenant.Status.ACTIVE and tenant.container_id and tenant.managed_identity_id:
        try:
            update_container_openrouter_key_secret(
                container_name=tenant.container_id,
                identity_id=tenant.managed_identity_id,
                kv_secret_name=secret_name,
            )
        except Exception as exc:
            # Container env-var update failed. DB fields are persisted so a
            # re-run won't re-create the sub-key (the eligibility filter
            # would skip this tenant), but the container is still pointed
            # at the SHARED secret — chat traffic will keep billing the
            # shared key, not the per-tenant one. Operator must retry the
            # rebind manually via update_container_openrouter_key_secret
            # for this specific container.
            stdout.write(
                f"[FAIL] tenant={tid} sub-key persisted in DB+KV but CONTAINER REBIND FAILED — "
                f"manual rebind required for {tenant.container_id}: {exc}"
            )
            logger.error("Container rebind failed for tenant=%s; manual retry needed", tid)
            return False

    stdout.write(f"[OK]   tenant={tid} hash={key_hash} secret={secret_name}")
    return True


class Command(BaseCommand):
    help = "Create per-tenant OpenRouter sub-keys for tenants missing one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            help="Backfill only the tenant with this UUID (default: all eligible)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen without making any changes",
        )

    def handle(self, *args, **options):
        if not getattr(settings, "OPENROUTER_PER_TENANT_KEYS_ENABLED", False):
            raise CommandError(
                "OPENROUTER_PER_TENANT_KEYS_ENABLED is False — refusing to "
                "create sub-keys whose env-var binding the runtime won't use. "
                "Flip the flag, then re-run."
            )

        tenants = _eligible_tenants(options.get("tenant"))
        if not tenants:
            self.stdout.write("Nothing to do — no eligible tenants without sub-keys.")
            return

        self.stdout.write(
            f"Backfilling sub-keys for {len(tenants)} tenant(s){' (dry-run)' if options['dry_run'] else ''}..."
        )
        ok = 0
        fail = 0
        for tenant in tenants:
            if _backfill_one(tenant, options["dry_run"], self.stdout):
                ok += 1
            else:
                fail += 1

        self.stdout.write(self.style.SUCCESS(f"Done: ok={ok} fail={fail}"))
