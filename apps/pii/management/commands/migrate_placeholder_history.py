"""Run the P3 W4 historical Layer-1 migration for one tenant."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.pii.historical_migration import (
    DEFAULT_BATCH_SIZE,
    migrate_tenant_registered_stores,
    normalize_batch_size,
    reset_store_cursor,
    w4_migration_tenant_allowed,
)
from apps.pii.store_registry import registered_store, registered_stores
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Migrate registered Layer-1 history to placeholder space (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id", required=True, help="Tenant UUID; there is intentionally no implicit fleet commit."
        )
        parser.add_argument("--store", help="Optional exact registered model label, for example journal.Task.")
        parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
        parser.add_argument(
            "--commit", action="store_true", help="Persist map bindings, rewritten fields, and receipts."
        )
        parser.add_argument(
            "--reset", action="store_true", help="Delete this mode's cursor before starting from the first PK."
        )

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(pk=options["tenant_id"])
        except (Tenant.DoesNotExist, ValidationError, ValueError) as exc:
            raise CommandError(f"Tenant {options['tenant_id']!r} not found") from exc

        try:
            batch_size = normalize_batch_size(options["batch_size"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        store_label = options.get("store")
        if store_label:
            try:
                registered_store(store_label)
            except LookupError as exc:
                raise CommandError(str(exc)) from exc

        from apps.tenants.middleware import set_rls_context

        set_rls_context(tenant_id=tenant.pk, service_role=True)
        if options["commit"] and not w4_migration_tenant_allowed(tenant):
            self.stdout.write(
                f"w4_migration_complete tenant={tenant.pk} mode=commit status=not_gated stores_complete=0 "
                "stores_skipped=0 batches=0"
            )
            return
        if options["reset"]:
            labels = (store_label,) if store_label else tuple(store.model_label for store in registered_stores())
            for label in labels:
                reset_store_cursor(tenant, label, commit=options["commit"])

        mode = "commit" if options["commit"] else "dry-run"
        self.stdout.write(f"w4_migration_start tenant={tenant.pk} mode={mode} batch_size={batch_size}")
        totals = migrate_tenant_registered_stores(
            tenant,
            commit=options["commit"],
            batch_size=batch_size,
            store_label=store_label,
        )
        self.stdout.write(
            self.style.SUCCESS(
                "w4_migration_complete "
                f"tenant={tenant.pk} mode={mode} stores_complete={totals['stores_complete']} "
                f"stores_skipped={totals['stores_skipped']} batches={totals['batches']}"
            )
        )
