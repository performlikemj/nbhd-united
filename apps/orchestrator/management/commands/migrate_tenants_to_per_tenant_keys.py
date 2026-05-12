"""Migrate existing tenants from the shared NBHD_INTERNAL_API_KEY to per-tenant keys (Phase 1c).

For each ACTIVE tenant that hasn't been migrated yet (`Tenant.internal_api_key == ""`):
  1. Generate a random per-tenant token.
  2. Write to Key Vault as `tenant-<uuid>-internal-key`.
  3. Grant the tenant's MI Key Vault Secrets User on the new secret.
  4. Update the Container App spec so the `nbhd-internal-api-key` secret
     ref points at the per-tenant KV secret (triggers new revision).
  5. Save the token to `Tenant.internal_api_key`.

Idempotent: tenants already migrated are skipped. Sequential per-tenant
with per-tenant error isolation — one tenant failing doesn't block others.

Dual-validation in `apps/integrations/internal_auth.py` (Phase 1a, PR #524)
keeps the container alive during the brief window between the DB save
and the revision rollout finishing.

Usage:
    python manage.py migrate_tenants_to_per_tenant_keys
    python manage.py migrate_tenants_to_per_tenant_keys --tenant-id <uuid>
    python manage.py migrate_tenants_to_per_tenant_keys --dry-run
    python manage.py migrate_tenants_to_per_tenant_keys --max 3
"""

from __future__ import annotations

import secrets as secrets_lib

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.orchestrator.azure_client import (
    assign_key_vault_role,
    get_identity_client,
    store_tenant_internal_key_in_key_vault,
    update_container_internal_api_key_secret,
)
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Migrate existing tenants from shared NBHD_INTERNAL_API_KEY to per-tenant keys (Phase 1c)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            help="Migrate only this tenant (UUID). Default: every unmigrated ACTIVE tenant.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without making any Azure / DB calls.",
        )
        parser.add_argument(
            "--max",
            type=int,
            default=None,
            help="Migrate at most this many tenants (useful for incremental rollout).",
        )

    def handle(self, *args, **options):
        candidates = self._candidates(options.get("tenant_id"))
        if options.get("max"):
            candidates = candidates[: options["max"]]

        self.stdout.write(f"Found {len(candidates)} tenant(s) needing migration")

        succeeded = 0
        failed = 0
        for tenant in candidates:
            if options["dry_run"]:
                self.stdout.write(f"[dry-run] would migrate {tenant.container_id} (id={tenant.id})")
                continue
            try:
                self._migrate_one(tenant)
                succeeded += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  FAIL {tenant.id} ({tenant.container_id}): {exc}"))

        if not options["dry_run"]:
            self.stdout.write(self.style.SUCCESS(f"Migrated: {succeeded}, Failed: {failed}"))

    def _candidates(self, tenant_id: str | None) -> list[Tenant]:
        q = (
            Tenant.objects.filter(
                internal_api_key="",
                status=Tenant.Status.ACTIVE,
            )
            .exclude(container_id="")
            .exclude(managed_identity_id="")
        )
        if tenant_id:
            q = q.filter(id=tenant_id)
        return list(q.order_by("created_at"))

    def _migrate_one(self, tenant: Tenant) -> None:
        self.stdout.write(f"[{tenant.container_id}] migrating tenant={tenant.id}")

        # 1. Generate token in memory.
        token = secrets_lib.token_urlsafe(48)

        # 2. Write to Key Vault. Returns "tenant-<uuid>-internal-key".
        kv_secret_name = store_tenant_internal_key_in_key_vault(str(tenant.id), token)
        self.stdout.write(f"  KV  wrote {kv_secret_name}")

        # 3. Grant per-secret Key Vault Secrets User to the tenant's MI.
        principal_id = self._lookup_principal_id(tenant)
        assign_key_vault_role(principal_id, secret_names=[kv_secret_name])
        self.stdout.write(f"  RBAC granted role on {kv_secret_name}")

        # 4. Rebind the container's nbhd-internal-api-key secret. New
        # revision is created; old revision stops once new is healthy.
        update_container_internal_api_key_secret(
            container_name=tenant.container_id,
            identity_id=tenant.managed_identity_id,
            kv_secret_name=kv_secret_name,
        )
        self.stdout.write(f"  CA   rebound nbhd-internal-api-key -> {kv_secret_name}")

        # 5. Save to DB last — if any earlier step failed the DB stays
        # empty and a retry regenerates everything cleanly.
        with transaction.atomic():
            tenant.internal_api_key = token
            tenant.save(update_fields=["internal_api_key", "updated_at"])
        self.stdout.write(self.style.SUCCESS("  DB   saved Tenant.internal_api_key"))

    def _lookup_principal_id(self, tenant: Tenant) -> str:
        """Resolve the tenant MI's principal_id via the Azure SDK.

        We only have `tenant.managed_identity_id` (the resource ID); the
        principal_id needs a fresh lookup. Used for the per-secret role
        assignment, which requires principal_id (not resource_id).
        """
        client = get_identity_client()
        mi_name = tenant.managed_identity_id.rsplit("/", 1)[-1]
        mi = client.user_assigned_identities.get(
            resource_group_name=settings.AZURE_RESOURCE_GROUP,
            resource_name=mi_name,
        )
        return mi.principal_id
