"""Retire enabled tenants that predate schedule-time welcome stamps."""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Stamp missing welcome markers for enabled features on active tenants without contacting tenant gateways."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print how many tenants and feature keys would be stamped without writing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        stamp = timezone.now().isoformat()
        feature_counts: Counter[str] = Counter()
        tenants_changed = 0
        tenants_scanned = 0

        tenants = Tenant.objects.filter(status=Tenant.Status.ACTIVE).iterator()
        for tenant in tenants:
            tenants_scanned += 1
            marks = dict(tenant.welcomes_sent or {})
            missing = []

            if tenant.fuel_enabled and not marks.get("fuel"):
                missing.append("fuel")
            if tenant.finance_active and not marks.get("finance"):
                missing.append("finance")
            if tenant.core_enabled and not marks.get("core"):
                missing.append("core")

            if not missing:
                continue

            tenants_changed += 1
            feature_counts.update(missing)
            if dry_run:
                continue

            for feature in missing:
                marks[feature] = stamp
            tenant.welcomes_sent = marks
            tenant.save(update_fields=["welcomes_sent"])

        action = "Would stamp" if dry_run else "Stamped"
        total_keys = feature_counts.total()
        self.stdout.write(
            f"{action} {total_keys} welcome key(s) across {tenants_changed} "
            f"of {tenants_scanned} active tenant(s): "
            f"fuel={feature_counts['fuel']}, "
            f"finance={feature_counts['finance']}, "
            f"core={feature_counts['core']}"
        )
