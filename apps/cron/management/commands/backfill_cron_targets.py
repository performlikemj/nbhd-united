"""Management command to backfill Telegram delivery targets for cron jobs."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Backfill cron jobs with missing Telegram delivery targets."

    def handle(self, *args, **options):
        tenants = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_fqdn__gt="",
            user__telegram_chat_id__isnull=False,
        )

        if not tenants.exists():
            self.stdout.write("No eligible tenants found.")
            return

        updated_total = 0

        for tenant in tenants:
            try:
                jobs_result = invoke_gateway_tool(tenant, "cron.list", {})
            except GatewayError as exc:
                self.stderr.write(
                    self.style.WARNING(
                        f"Skipping tenant {tenant.id} (container unavailable): {exc}"
                    )
                )
                continue

            jobs = jobs_result.get("jobs", [])
            if not isinstance(jobs, list):
                self.stderr.write(
                    self.style.WARNING(
                        f"Skipping tenant {tenant.id}: unexpected cron.list response shape"
                    )
                )
                continue

            tenant_updates = 0
            for job in jobs:
                if not isinstance(job, dict):
                    continue

                existing_delivery = job.get("delivery")
                if not isinstance(existing_delivery, dict):
                    continue

                if existing_delivery.get("mode") != "announce":
                    continue
                if existing_delivery.get("channel") != "telegram":
                    continue
                if existing_delivery.get("to"):
                    continue

                job_id = job.get("jobId") or job.get("name") or job.get("id")
                if not job_id:
                    self.stderr.write(
                        self.style.WARNING(
                            f"Tenant {tenant.id}: job missing identifier, skipping: {job}"
                        )
                    )
                    continue

                try:
                    invoke_gateway_tool(
                        tenant,
                        "cron.update",
                        {
                            "jobId": job_id,
                            "patch": {
                                "delivery": {
                                    **existing_delivery,
                                    "to": str(tenant.user.telegram_chat_id),
                                },
                            },
                        },
                    )
                    tenant_updates += 1
                    updated_total += 1
                    self.stdout.write(
                        f"Updated tenant {tenant.id}: job {job_id} delivery.to = {tenant.user.telegram_chat_id}"
                    )
                except GatewayError as exc:
                    self.stderr.write(
                        self.style.WARNING(
                            f"Failed tenant {tenant.id} job {job_id}: {exc}"
                        )
                    )

            if tenant_updates:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Tenant {tenant.id}: updated {tenant_updates} jobs."
                    )
                )
            else:
                self.stdout.write(
                    f"Tenant {tenant.id}: no updates needed."
                )

        self.stdout.write(
            self.style.SUCCESS(f"Backfill complete. Total updated jobs: {updated_total}")
        )
