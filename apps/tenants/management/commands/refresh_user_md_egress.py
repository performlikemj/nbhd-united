"""One-off repair for historical onboarding ``workspace/USER.md`` copies."""

from django.core.management.base import BaseCommand

from apps.orchestrator.azure_client import download_workspace_file, upload_workspace_file
from apps.tenants.envelope import render_safe_user_md
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Mint-redact and force-refresh USER.md for every provisioned tenant"

    def handle(self, *args, **options):
        tenants = Tenant.objects.select_related("user").filter(provisioned_at__isnull=False).order_by("id")

        refreshed = 0
        failed = 0
        for tenant in tenants.iterator():
            try:
                existing = download_workspace_file(str(tenant.id), "workspace/USER.md")
                content = render_safe_user_md(tenant, existing)
                if content is None:
                    failed += 1
                    continue
                upload_workspace_file(str(tenant.id), "workspace/USER.md", content)
                refreshed += 1
            except Exception:
                failed += 1

        self.stdout.write(f"tenants_refreshed={refreshed} tenants_failed={failed}")
