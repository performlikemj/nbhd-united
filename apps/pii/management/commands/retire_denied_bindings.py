"""Retire active entity-map bindings whose names are already denylisted."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.pii.entity_registry import retire_bindings_for_key


def _retire_for_denylist(entity_map: dict, denylist: dict, *, now_iso: str) -> tuple[dict, dict[str, int]]:
    updated_map = entity_map
    retired_by_key: dict[str, int] = {}
    for canonical in sorted(denylist):
        updated_map, placeholders = retire_bindings_for_key(
            updated_map,
            canonical,
            now_iso=now_iso,
        )
        if placeholders:
            retired_by_key[canonical] = len(placeholders)
    return updated_map, retired_by_key


class Command(BaseCommand):
    help = "Retire pii_entity_map bindings for existing pii_denylist keys (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("tenant_id", nargs="?", help="One tenant UUID")
        parser.add_argument("--all", action="store_true", help="Process every tenant")
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Persist retirements. Without this flag the command is a dry run.",
        )

    def handle(self, *args, **options):
        from apps.tenants.models import Tenant

        tenant_id = options["tenant_id"]
        all_tenants = options["all"]
        commit = options["commit"]
        if bool(tenant_id) == bool(all_tenants):
            raise CommandError("Provide exactly one tenant_id or --all.")

        tenant_ids = Tenant.objects.order_by("id").values_list("id", flat=True)
        if tenant_id:
            tenant_ids = tenant_ids.filter(pk=tenant_id)
        tenant_ids = list(tenant_ids)
        if tenant_id and not tenant_ids:
            raise CommandError(f"Unknown tenant: {tenant_id}")

        total_retired = 0
        tenants_with_matches = 0
        for pk in tenant_ids:
            if commit:
                with transaction.atomic():
                    tenant = (
                        Tenant.objects.select_for_update()
                        .only(
                            "id",
                            "pii_denylist",
                            "pii_entity_map",
                        )
                        .get(pk=pk)
                    )
                    entity_map, retired_by_key = _retire_for_denylist(
                        dict(tenant.pii_entity_map or {}),
                        dict(tenant.pii_denylist or {}),
                        now_iso=timezone.now().isoformat(),
                    )
                    if retired_by_key:
                        Tenant.objects.filter(pk=pk).update(pii_entity_map=entity_map)
            else:
                tenant = Tenant.objects.only("id", "pii_denylist", "pii_entity_map").get(pk=pk)
                _, retired_by_key = _retire_for_denylist(
                    dict(tenant.pii_entity_map or {}),
                    dict(tenant.pii_denylist or {}),
                    now_iso=timezone.now().isoformat(),
                )

            retired_count = sum(retired_by_key.values())
            total_retired += retired_count
            tenants_with_matches += bool(retired_count)
            grouped = ",".join(f"{key}={count}" for key, count in retired_by_key.items()) or "none"
            verb = "retired" if commit else "would_retire"
            self.stdout.write(f"tenant={tenant.id} {verb}={retired_count} by_key={grouped}")

        mode = "COMMIT" if commit else "DRY-RUN"
        total_label = "bindings_retired" if commit else "bindings_would_retire"
        self.stdout.write(
            self.style.SUCCESS(
                f"[{mode}] tenants_scanned={len(tenant_ids)} "
                f"tenants_with_matches={tenants_with_matches} {total_label}={total_retired}"
            )
        )
