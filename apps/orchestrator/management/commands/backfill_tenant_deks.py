"""Backfill per-tenant Data Encryption Keys (DEKs) for existing tenants (Encryption-at-rest Phase 1 PR5).

`provision_tenant` mints a tenant's DEK (wrapped under a freshly-created
per-tenant KEK) as of Phase 1 PR5. This command handles every tenant that
was provisioned BEFORE that step existed and therefore has no `TenantDek`
row yet.

For each candidate:
  1. Call `apps.crypto.keys.mint_and_wrap_dek(tenant)` — creates the KEK,
     generates a random 32-byte DEK, wraps it, and inserts the epoch-0
     `TenantDek` row. Idempotent by construction (a second call for the
     same tenant is a no-op), so this command is safe to re-run.
  2. Nothing else. Phase 1 ships dark — no ciphertext exists yet, no
     container/env change, no config bump. The DEK just needs to exist so
     Phase 2+ has something to encrypt under.

Idempotent: any tenant that already has an epoch-0 `TenantDek` row is
excluded by the candidate filter, so a re-run only touches tenants that
are still missing one. Sequential per-tenant with per-tenant error
isolation — one tenant failing (e.g. a KEK-vault throttle) doesn't block
the rest of the fleet.

Usage:
    python manage.py backfill_tenant_deks
    python manage.py backfill_tenant_deks --tenant-id <uuid>
    python manage.py backfill_tenant_deks --dry-run
    python manage.py backfill_tenant_deks --max 1
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Mint a Data Encryption Key (DEK) for every provisioned tenant that doesn't have one yet."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            help="Backfill only this tenant (UUID). Default: every provisioned tenant missing a DEK.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be minted without making any Azure / DB calls.",
        )
        parser.add_argument(
            "--max",
            type=int,
            default=None,
            help="Backfill at most this many tenants (useful for incremental/canary rollout).",
        )

    def handle(self, *args, **options):
        candidates = self._candidates(options.get("tenant_id"))
        if options.get("max"):
            candidates = candidates[: options["max"]]

        self.stdout.write(f"Found {len(candidates)} tenant(s) needing a DEK")

        succeeded = 0
        failed = 0
        for tenant in candidates:
            if options["dry_run"]:
                self.stdout.write(f"[dry-run] would mint DEK for {tenant.container_id or tenant.id} (id={tenant.id})")
                continue
            try:
                self._backfill_one(tenant)
                succeeded += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  FAIL {tenant.id} ({tenant.container_id}): {exc}"))

        if not options["dry_run"]:
            self.stdout.write(self.style.SUCCESS(f"Minted: {succeeded}, Failed: {failed}"))

    def _candidates(self, tenant_id: str | None) -> list[Tenant]:
        # "Provisioned" = ACTIVE or SUSPENDED (a suspended tenant's data is
        # still live in the DB and still needs a DEK for future encryption
        # phases — only DEPROVISIONING/DELETED tenants are excluded, and
        # PENDING/PROVISIONING tenants get their DEK minted by
        # provision_tenant itself once they finish provisioning).
        # `deks__isnull=True` is the reverse-FK "no TenantDek row" filter —
        # TenantDek.tenant has related_name="deks".
        q = Tenant.objects.filter(
            status__in=[Tenant.Status.ACTIVE, Tenant.Status.SUSPENDED],
            deks__isnull=True,
        )
        if tenant_id:
            q = q.filter(id=tenant_id)
        return list(q.order_by("created_at", "id"))

    def _backfill_one(self, tenant: Tenant) -> None:
        # Local import: apps.crypto is a new cross-app dependency this
        # command otherwise has no reason to import at module scope, and it
        # mirrors the same local-import pattern used in provision_tenant.
        from apps.crypto.keys import mint_and_wrap_dek

        self.stdout.write(f"[{tenant.container_id or tenant.id}] minting DEK for tenant={tenant.id}")
        row = mint_and_wrap_dek(tenant)
        self.stdout.write(self.style.SUCCESS(f"  DEK  minted epoch={row.dek_epoch} kek_version={row.kek_version}"))
