"""Detect and reap orphaned tenant container apps.

An orphan is an ``oc-*`` Azure Container App with no matching Tenant row (e.g.
a User account deletion whose Azure teardown was blocked by the prod
resource-group lock). See ``apps/orchestrator/orphan_reaper.py``.

Usage:
    # Dry run — list orphans + their awake state, change nothing:
    python manage.py reap_orphaned_containers --dry-run

    # Default — hibernate awake orphans (lock-safe), alert operator:
    python manage.py reap_orphaned_containers

    # Full teardown — also delete container/identity/share. Blocked by the prod
    # CanNotDelete locks unless an operator has lifted them first:
    python manage.py reap_orphaned_containers --apply
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.orchestrator.orphan_reaper import reap_orphaned_containers


class Command(BaseCommand):
    help = "Detect orphaned tenant containers (no Tenant row); hibernate awake ones; optionally tear down."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List orphans and their awake state; do not hibernate or delete.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Attempt full teardown (container, identity, file share). Blocked by prod locks unless lifted.",
        )
        parser.add_argument(
            "--no-alert",
            action="store_true",
            help="Do not send an admin alert even if orphans are found.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        apply = options["apply"]
        alert = not options["no_alert"]

        if dry_run and apply:
            self.stderr.write(self.style.ERROR("--dry-run and --apply are mutually exclusive."))
            return

        summary = reap_orphaned_containers(
            hibernate=not dry_run,
            apply=apply,
            alert=alert and not dry_run,
        )

        orphans = summary["orphans"]
        if not orphans:
            self.stdout.write(self.style.SUCCESS("No orphaned containers found."))
            return

        self.stdout.write(self.style.WARNING(f"Found {len(orphans)} orphaned container(s):"))
        for name in orphans:
            awake = name in summary["awake"]
            hibernated = name in summary["hibernated"]
            state = "AWAKE" if awake else "dormant"
            tail = ""
            if hibernated:
                tail = " -> hibernated"
            elif awake and dry_run:
                tail = " (would hibernate)"
            self.stdout.write(f"  • {name} [{state}]{tail}")
            if apply:
                self.stdout.write(f"      teardown: {json.dumps(summary['torn_down'].get(name, {}))}")

        if summary["errors"]:
            self.stderr.write(self.style.ERROR(f"Errors on: {', '.join(summary['errors'])}"))

        if not apply and not dry_run:
            self.stdout.write(
                "\nFull teardown not attempted. After lifting the relevant prod lock, "
                "re-run with --apply to delete the stranded resources."
            )
