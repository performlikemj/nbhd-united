"""Backfill internal_api_key_hash for tenants provisioned before automated flow.

Reads each tenant's per-tenant key from Key Vault and stores the SHA-256 hash
in the database so Django's internal auth can validate requests from the
tenant's OpenClaw container.

Usage:
    python manage.py backfill_internal_keys
    python manage.py backfill_internal_keys --dry-run
"""
from __future__ import annotations

import hashlib
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.orchestrator.azure_client import read_key_vault_secret
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill internal_api_key_hash from Key Vault for existing tenants"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without making changes",
        )

    def handle(self, **options):
        dry_run = options["dry_run"]
        tenants = Tenant.objects.exclude(status="deleted")
        updated = 0
        skipped = 0
        errors = 0

        for tenant in tenants:
            if tenant.internal_api_key_hash:
                self.stdout.write(f"  {tenant.id}: already has hash, skipping")
                skipped += 1
                continue

            secret_name = f"tenant-{tenant.id}-internal-key"
            try:
                key = read_key_vault_secret(secret_name)
            except Exception as exc:
                self.stderr.write(f"  {tenant.id}: Key Vault error: {exc}")
                errors += 1
                continue

            if not key:
                self.stderr.write(f"  {tenant.id}: no key found in Key Vault ({secret_name})")
                errors += 1
                continue

            key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()

            if dry_run:
                self.stdout.write(f"  {tenant.id}: would set hash {key_hash[:16]}...")
            else:
                tenant.internal_api_key_hash = key_hash
                tenant.internal_api_key_set_at = timezone.now()
                tenant.save(update_fields=["internal_api_key_hash", "internal_api_key_set_at", "updated_at"])
                self.stdout.write(f"  {tenant.id}: hash set {key_hash[:16]}...")

            updated += 1

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            f"\n{prefix}Done: {updated} updated, {skipped} skipped, {errors} errors"
        )
