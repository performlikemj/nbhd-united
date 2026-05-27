"""Reap orphan OpenRouter sub-keys + KV secrets (PR #1.6 Phase 5b).

Two failure modes can leave orphans behind:

  1. OR create_sub_key succeeded but KV write failed — orphan OR sub-key
     with no Tenant row referencing its hash.
  2. KV write succeeded but Tenant.save failed — orphan KV secret with
     no Tenant row referencing it.

This sweeper enumerates OR sub-keys via the management API, cross-
references each against the Tenant table (matching on
``openrouter_key_hash``), and DELETEs any OR-side key whose hash is
absent from the Tenant table AND whose created_at is older than the
``--min-age-hours`` threshold (default 24h). The age gate prevents the
sweeper from racing a tenant provision in flight.

KV orphan cleanup isn't included here — the Azure SDK paths for
listing/deleting secrets by prefix vary by Vault SKU, and the safer
move is operator-driven via ``az keyvault secret list``. Reach out
to MJ if you need that step automated.

Usage:

    python manage.py sweep_orphan_openrouter_keys              # 24h age threshold
    python manage.py sweep_orphan_openrouter_keys --min-age-hours 1
    python manage.py sweep_orphan_openrouter_keys --dry-run    # report only
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def _parse_or_timestamp(value) -> datetime | None:
    """OR returns timestamps as ISO 8601 strings; some endpoints use Z, some +00:00."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        # Python 3.11+ handles "Z" but be defensive.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


class Command(BaseCommand):
    help = "Reap OpenRouter sub-keys that have no matching Tenant row."

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-age-hours",
            type=int,
            default=24,
            help="Don't delete keys younger than this (protects in-flight provisioning). Default 24h.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without calling DELETE.",
        )

    def handle(self, *args, **options):
        from apps.billing.openrouter_admin import OpenRouterAdminError, delete_sub_key, list_sub_keys

        min_age = timedelta(hours=options["min_age_hours"])
        cutoff = datetime.now(UTC) - min_age

        try:
            all_keys = list_sub_keys(include_disabled=True)
        except OpenRouterAdminError as exc:
            raise CommandError(f"list_sub_keys failed: {exc}") from exc

        known_hashes = set(
            Tenant.objects.exclude(openrouter_key_hash="")
            .values_list("openrouter_key_hash", flat=True)
        )

        orphan_count = 0
        deleted = 0
        skipped_young = 0
        failed = 0

        for entry in all_keys:
            if not isinstance(entry, dict):
                continue
            key_hash = (entry.get("hash") or "").strip()
            if not key_hash or key_hash in known_hashes:
                continue
            created = _parse_or_timestamp(entry.get("created_at"))
            label = entry.get("label") or entry.get("name") or "?"
            orphan_count += 1

            if created is None or created > cutoff:
                skipped_young += 1
                self.stdout.write(
                    f"[skip-young] hash={key_hash[:12]}… label={label!r} created={created}"
                )
                continue

            if options["dry_run"]:
                self.stdout.write(
                    f"[DRY-RUN] would delete orphan hash={key_hash[:12]}… label={label!r} created={created}"
                )
                deleted += 1
                continue

            try:
                delete_sub_key(key_hash)
                deleted += 1
                self.stdout.write(f"[deleted] hash={key_hash[:12]}… label={label!r}")
            except OpenRouterAdminError as exc:
                failed += 1
                self.stdout.write(f"[fail] hash={key_hash[:12]}… {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: total_keys={len(all_keys)} orphans={orphan_count} "
                f"deleted={deleted} skipped_young={skipped_young} failed={failed}"
            )
        )
