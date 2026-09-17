"""READ-ONLY drift check: compare every tenant's LIVE Azure Container App
template against the code-declared OpenClaw desired state, and page MJ via
Pushover (one consolidated message) if anything real drifted.

"Real" = storage / env fields (workspace mountOptions, oc-state volume +
mount, relocation env vars), a Tenant.container_image_tag that disagrees with
the image Azure actually runs, or a template that could not be read at all.
A tenant simply not being on ``OPENCLAW_IMAGE_TAG`` yet is EXPECTED during a
staged rollout and is reported as ``stale-gated`` without alerting.

Nothing here writes to Azure or touches tenant data. The fix is always:

    python manage.py ensure_openclaw_ready --all

Usage:

    python manage.py detect_openclaw_drift --all             # sweep + alert if drifted
    python manage.py detect_openclaw_drift --all --no-alert  # sweep, print only
    python manage.py detect_openclaw_drift --tenant <uuid> --json

The same sweep runs daily from the ``detect-openclaw-drift`` QStash cron once
``OPENCLAW_DRIFT_ALERTS_ENABLED=true`` (default off — deploying this never
starts paging on its own). Runbook: docs/agents/openclaw-fleet-admin.md
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.azure_client import is_mock
from apps.orchestrator.openclaw_drift import (
    FleetDriftReport,
    active_tenant_queryset,
    check_fleet_drift,
    format_alert,
    send_drift_alert,
)
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Read-only: report OpenClaw desired-state drift per tenant and send one consolidated Pushover alert."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--tenant", help="Single tenant UUID")
        target.add_argument("--all", action="store_true", help="All active, non-hibernated tenants")
        parser.add_argument("--json", action="store_true", help="Emit the full report as JSON (no alert text)")
        parser.add_argument("--no-alert", action="store_true", help="Never send the Pushover alert")

    def handle(self, *args, **options):
        if is_mock():
            if options["json"]:
                self.stdout.write(json.dumps(FleetDriftReport(mock=True).as_dict(), indent=2))
            else:
                self.stdout.write("[MOCK] AZURE_MOCK=true — nothing checked.")
            return

        if options["tenant"]:
            tenants = list(Tenant.objects.filter(id=options["tenant"], container_id__gt=""))
            if not tenants:
                raise CommandError(f"No provisioned tenant with id {options['tenant']}")
        else:
            tenants = list(active_tenant_queryset())

        report = check_fleet_drift(tenants)
        alert_status = "skipped"
        if not options["no_alert"]:
            alert_status = send_drift_alert(report)

        if options["json"]:
            payload = report.as_dict()
            payload["alert_status"] = alert_status
            self.stdout.write(json.dumps(payload, indent=2))
            return

        self.stdout.write(f"Checked {report.checked} tenant(s): {len(report.drifted)} drifted")
        for t in report.tenants:
            if not t.has_drift:
                continue
            label = f"{t.container_name} ({t.short_id})"
            if t.error:
                self.stderr.write(self.style.ERROR(f"  {label}: UNREADABLE — {t.error}"))
                continue
            style = self.style.WARNING if t.should_alert else self.style.NOTICE
            self.stdout.write(style(f"  {label}: {'DRIFTED' if t.should_alert else 'gated image only'}"))
            for d in t.fields:
                tag = "ALERT" if d.alert else "info"
                note = f" ({d.note})" if d.note else ""
                self.stdout.write(f"    [{tag}] {d.field}  expected={d.expected}  actual={d.actual}{note}")

        if report.alerting:
            self.stdout.write("")
            self.stdout.write(format_alert(report))
            self.stdout.write(f"Pushover: {alert_status}")
        else:
            self.stdout.write(self.style.SUCCESS("No alert-worthy drift."))
