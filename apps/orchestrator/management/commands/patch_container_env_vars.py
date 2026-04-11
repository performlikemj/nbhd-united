"""Patch NODE_OPTIONS and OPENCLAW_DISABLE_BONJOUR on existing containers.

One-time remediation command. Azure Container Apps caches Dockerfile ENV
from first provisioning — containers provisioned before the --require and
OPENCLAW_DISABLE_BONJOUR fixes were added are missing them.

Also audits volume mounts when --audit-volumes is passed, comparing against
the canonical template in create_container_app().
"""
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.orchestrator.azure_client import (
    get_container_client,
    update_container_env_var,
)
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

EXPECTED_NODE_OPTIONS = (
    "--max-old-space-size=1024 "
    "--dns-result-order=ipv4first "
    "--no-network-family-autoselection "
    "--require /opt/nbhd/suppress-chmod-eperm.js"
)

CANONICAL_VOLUMES = {"workspace", "sessions-scratch"}
CANONICAL_MOUNTS = {
    "/home/node/.openclaw": "workspace",
    "/home/node/.openclaw/agents": "sessions-scratch",
}


class Command(BaseCommand):
    help = "Patch env vars and audit volumes on existing OpenClaw containers"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without modifying anything",
        )
        parser.add_argument(
            "--audit-volumes", action="store_true",
            help="Check for extra/missing volume mounts vs canonical template",
        )
        parser.add_argument(
            "--tenant-id", type=str, default="",
            help="Patch a single tenant (UUID)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        audit_volumes = options["audit_volumes"]
        tenant_id = (options.get("tenant_id") or "").strip()

        queryset = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            container_id__gt="",
            hibernated_at__isnull=True,
        )
        if tenant_id:
            queryset = queryset.filter(id=tenant_id)

        tenants = list(queryset)
        if not tenants:
            self.stdout.write("No matching tenants found.")
            return

        client = get_container_client()
        if client is None:
            self.stdout.write("AZURE_MOCK is set — skipping.")
            return

        patched = 0
        skipped = 0
        volume_issues = []

        for tenant in tenants:
            container_name = tenant.container_id
            try:
                app = client.container_apps.get(
                    settings.AZURE_RESOURCE_GROUP, container_name,
                )
            except Exception as exc:
                self.stderr.write(f"  SKIP {container_name}: {exc}")
                skipped += 1
                continue

            container = None
            for c in app.template.containers:
                if c.name == "openclaw":
                    container = c
                    break

            if container is None:
                self.stderr.write(f"  SKIP {container_name}: no 'openclaw' container found")
                skipped += 1
                continue

            env_map = {}
            for entry in (container.env or []):
                env_map[entry.name] = entry.value or (
                    f"secretRef:{entry.secret_ref}" if hasattr(entry, "secret_ref") and entry.secret_ref else ""
                )

            needs_patch = []

            # Check NODE_OPTIONS
            current_node_opts = env_map.get("NODE_OPTIONS", "")
            if "suppress-chmod-eperm" not in current_node_opts:
                needs_patch.append(("NODE_OPTIONS", EXPECTED_NODE_OPTIONS, current_node_opts))

            # Check OPENCLAW_DISABLE_BONJOUR
            current_bonjour = env_map.get("OPENCLAW_DISABLE_BONJOUR", "")
            if current_bonjour != "1":
                needs_patch.append(("OPENCLAW_DISABLE_BONJOUR", "1", current_bonjour))

            if needs_patch:
                for env_name, desired, current in needs_patch:
                    tag = "DRY-RUN" if dry_run else "PATCH"
                    self.stdout.write(
                        f"  [{tag}] {container_name}: {env_name} "
                        f"'{current[:60]}...' → '{desired[:60]}...'"
                    )
                    if not dry_run:
                        update_container_env_var(container_name, env_name, desired)
                patched += 1
            else:
                self.stdout.write(f"  OK {container_name}: env vars correct")

            # Volume audit
            if audit_volumes:
                volumes = {v.name for v in (app.template.volumes or [])}
                mounts = {}
                for vm in (container.volume_mounts or []):
                    mounts[vm.mount_path] = vm.volume_name

                extra_vols = volumes - CANONICAL_VOLUMES
                missing_vols = CANONICAL_VOLUMES - volumes
                extra_mounts = set(mounts.keys()) - set(CANONICAL_MOUNTS.keys())

                if extra_vols or missing_vols or extra_mounts:
                    issue = {
                        "container": container_name,
                        "tenant_id": str(tenant.id),
                        "extra_volumes": sorted(extra_vols),
                        "missing_volumes": sorted(missing_vols),
                        "extra_mounts": sorted(extra_mounts),
                    }
                    volume_issues.append(issue)
                    self.stdout.write(
                        f"  VOLUME ISSUE {container_name}: "
                        f"extra_vols={sorted(extra_vols)} "
                        f"missing_vols={sorted(missing_vols)} "
                        f"extra_mounts={sorted(extra_mounts)}"
                    )

        self.stdout.write(
            f"\nDone: {len(tenants)} checked, {patched} patched, {skipped} skipped"
        )
        if volume_issues:
            self.stdout.write(
                f"\n{len(volume_issues)} containers with volume issues "
                f"(may need deprovision/reprovision):"
            )
            for issue in volume_issues:
                self.stdout.write(f"  {issue['container']} (tenant {issue['tenant_id']})")
