"""Diagnostic command to check every link in the Gateway chain for a tenant."""
from __future__ import annotations

import socket

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Check each link in the cron→Gateway→journal chain for a tenant. "
        "Stops at first failure so you can fix issues one at a time."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=str,
            default="",
            help="Tenant UUID. If omitted, uses the first active tenant.",
        )

    def handle(self, *args, **options):
        tenant = self._resolve_tenant(options["tenant_id"])
        checks = [
            self._check_tenant,
            self._check_dns,
            self._check_key_vault,
            self._check_gateway_reachable,
            self._check_tools_invoke,
            self._check_cron_jobs_seeded,
            self._check_runtime_endpoint,
        ]
        for check in checks:
            passed = check(tenant)
            if not passed:
                self.stderr.write(self.style.ERROR("\nStopped at first failure."))
                return

        self.stdout.write(self.style.SUCCESS("\nAll checks passed."))

    # ------------------------------------------------------------------

    def _resolve_tenant(self, tenant_id: str) -> Tenant:
        if tenant_id:
            try:
                return Tenant.objects.select_related("user").get(id=tenant_id)
            except (Tenant.DoesNotExist, ValueError) as exc:
                raise CommandError(f"Tenant not found: {tenant_id}") from exc
        tenant = Tenant.objects.filter(status=Tenant.Status.ACTIVE).select_related("user").first()
        if not tenant:
            raise CommandError("No active tenants found. Pass --tenant-id explicitly.")
        return tenant

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_tenant(self, tenant: Tenant) -> bool:
        self.stdout.write("\n1. Tenant exists & active")
        self.stdout.write(f"   id:             {tenant.id}")
        self.stdout.write(f"   user:           {tenant.user.display_name}")
        self.stdout.write(f"   status:         {tenant.status}")
        self.stdout.write(f"   container_id:   {tenant.container_id or '(empty)'}")
        self.stdout.write(f"   container_fqdn: {tenant.container_fqdn or '(empty)'}")

        if tenant.status != Tenant.Status.ACTIVE:
            self.stderr.write(self.style.ERROR(f"   FAIL — tenant status is {tenant.status}, expected active"))
            return False
        if not tenant.container_fqdn:
            self.stderr.write(self.style.ERROR("   FAIL — container_fqdn is empty"))
            return False

        self.stdout.write(self.style.SUCCESS("   PASS"))
        return True

    def _check_dns(self, tenant: Tenant) -> bool:
        self.stdout.write("\n2. Container FQDN resolves (DNS)")
        fqdn = tenant.container_fqdn
        try:
            addrs = socket.getaddrinfo(fqdn, 443)
            ip = addrs[0][4][0] if addrs else "(unknown)"
            self.stdout.write(f"   {fqdn} → {ip}")
            self.stdout.write(self.style.SUCCESS("   PASS"))
            return True
        except socket.gaierror as exc:
            self.stderr.write(self.style.ERROR(f"   FAIL — DNS lookup failed: {exc}"))
            return False

    def _check_key_vault(self, tenant: Tenant) -> bool:
        self.stdout.write("\n3. Key Vault secret readable")
        from apps.orchestrator.azure_client import read_key_vault_secret

        secret_name = f"tenant-{tenant.id}-internal-key"
        self.stdout.write(f"   secret: {secret_name}")
        value = read_key_vault_secret(secret_name)
        if value:
            self.stdout.write(f"   length: {len(value)} chars")
            self.stdout.write(self.style.SUCCESS("   PASS"))
            return True
        else:
            self.stderr.write(self.style.ERROR("   FAIL — secret is None or empty"))
            return False

    def _check_gateway_reachable(self, tenant: Tenant) -> bool:
        self.stdout.write("\n4. Gateway reachable (health check)")
        import requests

        url = f"https://{tenant.container_fqdn}/health"
        self.stdout.write(f"   GET {url}")
        try:
            resp = requests.get(url, timeout=5)
            self.stdout.write(f"   status: {resp.status_code}")
            if resp.status_code == 200:
                self.stdout.write(self.style.SUCCESS("   PASS"))
                return True
            else:
                self.stderr.write(self.style.ERROR(f"   FAIL — expected 200, got {resp.status_code}"))
                return False
        except requests.RequestException as exc:
            self.stderr.write(self.style.ERROR(f"   FAIL — {exc}"))
            return False

    def _check_tools_invoke(self, tenant: Tenant) -> bool:
        self.stdout.write("\n5. tools/invoke works (cron.list)")
        from apps.cron.gateway_client import GatewayError, invoke_gateway_tool

        try:
            result = invoke_gateway_tool(tenant, "cron.list", {})
            job_count = len(result.get("jobs", []))
            self.stdout.write(f"   returned {job_count} jobs")
            self.stdout.write(self.style.SUCCESS("   PASS"))
            return True
        except GatewayError as exc:
            self.stderr.write(self.style.ERROR(f"   FAIL — {exc}"))
            return False

    def _check_cron_jobs_seeded(self, tenant: Tenant) -> bool:
        self.stdout.write("\n6. Cron jobs seeded (Azure File Share)")
        import json as _json

        from django.conf import settings

        from apps.orchestrator.azure_client import _is_mock, get_storage_client

        tenant_id = str(tenant.id)
        share_name = f"ws-{tenant_id[:20]}"

        if _is_mock():
            self.stdout.write("   (mock mode — skipping file share check)")
            self.stdout.write(self.style.SUCCESS("   PASS (mock)"))
            return True

        account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
        if not account_name:
            self.stderr.write(self.style.ERROR("   FAIL — AZURE_STORAGE_ACCOUNT_NAME not configured"))
            return False

        try:
            from azure.storage.fileshare import ShareFileClient

            storage_client = get_storage_client()
            keys = storage_client.storage_accounts.list_keys(
                settings.AZURE_RESOURCE_GROUP, account_name,
            )
            account_key = keys.keys[0].value

            file_client = ShareFileClient(
                account_url=f"https://{account_name}.file.core.windows.net",
                share_name=share_name,
                file_path="cron/jobs.json",
                credential=account_key,
            )
            data = file_client.download_file().readall()
            parsed = _json.loads(data)
            jobs = parsed.get("jobs", [])
            self.stdout.write(f"   share: {share_name}, file: cron/jobs.json")
            self.stdout.write(f"   jobs found: {len(jobs)}")
            if jobs:
                for j in jobs:
                    self.stdout.write(f"     - {j.get('name', '?')} ({j.get('schedule', {}).get('expr', '?')})")
                self.stdout.write(self.style.SUCCESS("   PASS"))
                return True
            else:
                self.stderr.write(self.style.ERROR("   FAIL — jobs.json exists but has 0 jobs"))
                return False
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"   FAIL — could not read cron/jobs.json: {exc}"))
            return False

    def _check_runtime_endpoint(self, tenant: Tenant) -> bool:
        self.stdout.write("\n7. Runtime endpoint reachable (daily-note append)")
        import requests

        from apps.orchestrator.azure_client import read_key_vault_secret

        secret_name = f"tenant-{tenant.id}-internal-key"
        token = read_key_vault_secret(secret_name)
        if not token:
            self.stderr.write(self.style.ERROR("   FAIL — cannot read internal key (already failed in step 3?)"))
            return False

        # Use the container FQDN to hit the runtime endpoint on the Gateway,
        # which proxies back to Django. This tests the full round-trip.
        url = (
            f"https://{tenant.container_fqdn}"
            f"/api/v1/integrations/runtime/{tenant.id}/daily-note/append/"
        )
        self.stdout.write(f"   POST {url}")
        try:
            resp = requests.post(
                url,
                json={
                    "content": "[health-check] Gateway→Django round-trip OK",
                    "date": "1970-01-01",
                    "section_slug": "health-check",
                },
                headers={
                    "X-NBHD-Internal-Key": token,
                    "X-NBHD-Tenant-Id": str(tenant.id),
                },
                timeout=10,
            )
            self.stdout.write(f"   status: {resp.status_code}")
            if resp.status_code in (200, 201):
                self.stdout.write(self.style.SUCCESS("   PASS"))
                return True
            else:
                self.stderr.write(self.style.ERROR(
                    f"   FAIL — expected 200/201, got {resp.status_code}: {resp.text[:300]}"
                ))
                return False
        except requests.RequestException as exc:
            self.stderr.write(self.style.ERROR(f"   FAIL — {exc}"))
            return False
