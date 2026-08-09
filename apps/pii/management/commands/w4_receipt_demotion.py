"""Preflight and optionally demote known W4 false-clean receipts."""

from __future__ import annotations

from collections import Counter

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.pii.historical_migration import DEFAULT_BATCH_SIZE, normalize_batch_size, w4_migration_tenant_allowed
from apps.pii.receipt_demotion import parse_deploy_cutoff, process_receipt_demotion_batch
from apps.pii.store_registry import registered_store, registered_stores
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Demote pre-W4 lying placeholder receipts (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True)
        parser.add_argument("--deploy-cutoff", required=True, help="Timezone-aware d24cf4b5 production deploy time.")
        parser.add_argument("--store", help="Optional exact registered model label.")
        parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(pk=options["tenant_id"])
        except (Tenant.DoesNotExist, ValidationError, ValueError) as exc:
            raise CommandError(f"Tenant {options['tenant_id']!r} not found") from exc
        try:
            cutoff = parse_deploy_cutoff(options["deploy_cutoff"])
            batch_size = normalize_batch_size(options["batch_size"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        store_label = options.get("store")
        if store_label:
            try:
                stores = (registered_store(store_label),)
            except LookupError as exc:
                raise CommandError(str(exc)) from exc
        else:
            stores = registered_stores()

        from apps.tenants.middleware import set_rls_context

        set_rls_context(tenant_id=tenant.pk, service_role=True)
        if options["commit"] and not w4_migration_tenant_allowed(tenant):
            self.stdout.write(f"w4_receipt_demotion_complete tenant={tenant.pk} mode=commit status=not_gated")
            return

        mode = "commit" if options["commit"] else "dry-run"
        for store in stores:
            after_pk = ""
            totals: Counter[str] = Counter()
            while True:
                result = process_receipt_demotion_batch(
                    tenant,
                    store.model_label,
                    cutoff,
                    commit=options["commit"],
                    batch_size=batch_size,
                    after_pk=after_pk,
                )
                totals.update(result.counts)
                after_pk = result.last_pk
                if result.done or result.skipped:
                    break
            self.stdout.write(
                "w4_receipt_demotion_store "
                f"tenant={tenant.pk} store={store.model_label} mode={mode} matched={totals['matched']} "
                f"runtime_pre_cutoff={totals['runtime_pre_cutoff']} no_leaf_shape={totals['no_leaf_shape']} "
                f"demoted={totals['demoted']} changed_skipped={totals['changed_skipped']}"
            )
