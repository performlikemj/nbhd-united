"""Force-refresh ``workspace/USER.md`` for active tenants.

Used after deploying a workspace_envelope change to backfill USER.md across
the fleet without waiting for organic post_save signals to fire. Each call
uses ``force=True`` so the leading-edge debounce is bypassed.

Usage:
    python manage.py refresh_user_md                      # all active tenants
    python manage.py refresh_user_md --tenant <uuid>      # single tenant
    python manage.py refresh_user_md --tenant <uuid-prefix>  # uuid prefix match
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.orchestrator.workspace_envelope import push_user_md
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Refresh workspace/USER.md for active tenants"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            type=str,
            default="",
            help="Limit to a single tenant by UUID (or UUID prefix).",
        )

    def handle(self, *args, **options):
        qs = Tenant.objects.select_related("user").filter(status=Tenant.Status.ACTIVE).exclude(container_id="")

        tenant_arg = (options.get("tenant") or "").strip()
        if tenant_arg:
            qs = qs.filter(id__startswith=tenant_arg)

        tenants = list(qs)
        total = len(tenants)
        self.stdout.write(f"Refreshing USER.md for {total} tenant(s)...")

        pushed = 0
        failed = 0
        for tenant in tenants:
            try:
                push_user_md(tenant, force=True)
                pushed += 1
                self.stdout.write(f"  ✅ {str(tenant.id)[:8]}")
            except Exception as exc:
                failed += 1
                self.stdout.write(self.style.ERROR(f"  ❌ {str(tenant.id)[:8]}: {exc}"))

        self.stdout.write(self.style.SUCCESS(f"Done: {pushed} pushed, {failed} errors"))
