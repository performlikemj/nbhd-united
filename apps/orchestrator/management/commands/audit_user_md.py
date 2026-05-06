"""Read-only audit of ``workspace/USER.md`` across active tenants.

Classifies each tenant's USER.md as one of:
  - ``missing``      — file does not exist on the share
  - ``empty``        — present but blank
  - ``boilerplate``  — matches OpenClaw's default seeded content
  - ``managed``      — already contains the NBHD platform-managed sentinels
  - ``agent``        — has content but no platform sentinels (would be
                       preserved by the merge on first refresh)

Helps decide migration order and surface tenants that have meaningful
agent-written content before fleet rollout.

Usage:
    python manage.py audit_user_md
    python manage.py audit_user_md --verbose   # also prints first 200 chars of agent-only content
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.orchestrator.azure_client import download_workspace_file
from apps.orchestrator.workspace_envelope import (
    _OPENCLAW_DEFAULT_USER_MD,
    BEGIN_MARKER,
    END_MARKER,
)
from apps.tenants.models import Tenant


def _classify(content: str | None) -> str:
    if content is None:
        return "missing"
    stripped = content.strip()
    if not stripped:
        return "empty"
    if stripped == _OPENCLAW_DEFAULT_USER_MD.strip():
        return "boilerplate"
    if BEGIN_MARKER in content and END_MARKER in content:
        return "managed"
    return "agent"


class Command(BaseCommand):
    help = "Audit workspace/USER.md state across active tenants"

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print first 200 chars of agent-only content for inspection",
        )

    def handle(self, *args, **options):
        verbose = options.get("verbose", False)
        tenants = Tenant.objects.filter(status=Tenant.Status.ACTIVE).exclude(container_id="").order_by("id")

        counts: dict[str, int] = {
            "missing": 0,
            "empty": 0,
            "boilerplate": 0,
            "managed": 0,
            "agent": 0,
            "errors": 0,
        }

        for tenant in tenants:
            tenant_id = str(tenant.id)
            try:
                content = download_workspace_file(tenant_id, "workspace/USER.md")
            except Exception as exc:
                counts["errors"] += 1
                self.stdout.write(self.style.ERROR(f"  ❌ {tenant_id[:8]}: {exc}"))
                continue

            classification = _classify(content)
            counts[classification] += 1

            line = f"  {classification:11s}  {tenant_id[:8]}"
            if classification == "agent":
                self.stdout.write(self.style.WARNING(line))
                if verbose and content:
                    preview = content.strip().replace("\n", " ⏎ ")[:200]
                    self.stdout.write(f"             preview: {preview}")
            elif classification == "managed":
                self.stdout.write(self.style.SUCCESS(line))
            else:
                self.stdout.write(line)

        self.stdout.write("")
        self.stdout.write("Summary:")
        for key in ("missing", "empty", "boilerplate", "managed", "agent", "errors"):
            self.stdout.write(f"  {key:11s}  {counts[key]}")
