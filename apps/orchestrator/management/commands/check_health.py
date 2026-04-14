"""Check health of all active tenant containers."""

import json

from django.core.management.base import BaseCommand

from apps.orchestrator.services import check_all_tenants_health
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Check health of all active OpenClaw instances"

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Output structured JSON")

    def handle(self, *args, **options):
        tenants = Tenant.objects.filter(status=Tenant.Status.ACTIVE)

        if not tenants.exists():
            if options["json"]:
                self.stdout.write(json.dumps({"tenants": 0, "results": []}))
            else:
                self.stdout.write("No active tenants.")
            return

        results = check_all_tenants_health()

        if options["json"]:
            self.stdout.write(json.dumps({"tenants": len(results), "results": results}, indent=2))
            return

        for result in results:
            icon = "\u2705" if result["healthy"] else "\u274c"
            name = result.get("display_name", "?")
            container = result.get("container", "(none)")
            self.stdout.write(f"{icon}  {result['tenant_id']}  {name:<20}  {container}")

            for check_name, check_result in result.get("checks", {}).items():
                detail = check_result.get("detail", "")
                rt = check_result.get("response_time_ms")
                status = "\u2705" if check_result["ok"] else "\u274c"
                extra = f" ({rt}ms)" if rt else f" — {detail}" if detail else ""
                self.stdout.write(f"    {status} {check_name}{extra}")
