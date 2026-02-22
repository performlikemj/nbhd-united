"""Disable tenant OpenClaw-side Telegram polling by clearing Telegram env variables."""

from django.core.management.base import BaseCommand
from django.db import connection

from apps.orchestrator.azure_client import update_container_env_var


class Command(BaseCommand):
    help = (
        "Disable Telegram polling on tenant OpenClaw containers by removing "
        "TELEGRAM_BOT_TOKEN and OPENCLAW_WEBHOOK_SECRET from env."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            action="append",
            dest="tenant_ids",
            help="Target a single tenant UUID. Repeat to patch multiple tenants.",
        )
        parser.add_argument(
            "--all-active",
            action="store_true",
            help="Patch all tenants with status='active' and non-empty container_id.",
        )

    def handle(self, *args, **options):
        tenant_ids = options.get("tenant_ids") or []
        all_active = options.get("all_active")

        tenant_containers = []

        if all_active:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT id, container_id FROM tenants WHERE status='active' AND container_id <> ''"
                )
                tenant_containers.extend(cur.fetchall())
        elif tenant_ids:
            with connection.cursor() as cur:
                for tenant_id in tenant_ids:
                    cur.execute(
                        "SELECT id, container_id FROM tenants WHERE id = %s AND status='active' AND container_id <> ''",
                        [tenant_id],
                    )
                    row = cur.fetchone()
                    if row:
                        tenant_containers.append(row)
                    else:
                        self.stdout.write(
                            self.style.WARNING(f"Tenant {tenant_id} not active or has no container_id")
                        )
        else:
            self.stdout.write(
                self.style.ERROR("Specify either --tenant-id (repeatable) or --all-active")
            )
            return

        if not tenant_containers:
            self.stdout.write(self.style.WARNING("No matching tenant containers found."))
            return

        patched = 0
        failed = 0

        for tenant_id, container_id in tenant_containers:
            try:
                update_container_env_var(container_id, "TELEGRAM_BOT_TOKEN", "")
                update_container_env_var(container_id, "OPENCLAW_WEBHOOK_SECRET", "")
                self.stdout.write(f"✅ patched tenant={tenant_id} container={container_id}")
                patched += 1
            except Exception as exc:
                self.stdout.write(
                    self.style.ERROR(f"❌ failed tenant={tenant_id} container={container_id}: {exc}")
                )
                failed += 1

        self.stdout.write(self.style.SUCCESS(f"Done: patched={patched}, failed={failed}"))
