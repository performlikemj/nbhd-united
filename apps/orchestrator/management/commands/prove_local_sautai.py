"""Drive the real installed plugin, runtime view, job and local sim client."""

import json
import os
import time
from datetime import date

import httpx
from django.core.management.base import BaseCommand, CommandError

from apps.integrations.models import SautaiMealPlanJob
from apps.orchestrator.gateway_url import gateway_base_url
from apps.orchestrator.local_test import local_root
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Local sim proof; requires the operator-confirmed fixture week (Monday)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirmed-week", required=True, help="Explicit consent for this synthetic week, YYYY-MM-DD"
        )

    def handle(self, *args, **options):
        tid = os.environ["NBHD_TENANT_ID"]
        if local_root(tid) is None:
            raise CommandError("Requires isolated synthetic local stack")
        tenant = Tenant.objects.get(id=tid)
        if gateway_base_url(tenant) != "http://127.0.0.1:19443":
            raise CommandError("Unexpected gateway")
        week = date.fromisoformat(options["confirmed_week"])
        if week.weekday() != 0:
            raise CommandError("Use a Monday")
        if SautaiMealPlanJob.objects.filter(tenant=tenant, week_start=week).exists():
            raise CommandError("Choose an unused fixture week; proof must create a fresh job")
        headers = {"Authorization": "Bearer " + tenant.internal_api_key}
        body = {
            "week_start": week.isoformat(),
            "number_of_days": 1,
            "user_prompt": "[NBHD E2E SYNTHETIC] Prepare a simple local simulation meal plan.",
        }
        with httpx.Client(timeout=150, follow_redirects=False, trust_env=False) as client:

            def invoke(parameters):
                response = client.post(
                    gateway_base_url(tenant) + "/tools/invoke",
                    headers=headers,
                    json={"tool": "nbhd_generate_meal_plan", "args": parameters},
                )
                if response.status_code != 200:
                    raise CommandError("Plugin invocation failed")
                envelope = response.json()
                if envelope.get("ok") is not True:
                    raise CommandError("Gateway rejected tool")
                payload = envelope.get("result", {}).get("details", {}).get("json")
                if not isinstance(payload, dict):
                    raise CommandError("Plugin did not return structured runtime response")
                return payload

            preview = invoke(body)
            if preview.get("status") != "confirmation_required":
                raise CommandError("No preview; check sim hand-off and account link")
            parameters = preview["preview"]["tool_parameters"]
            if parameters.get("week_start") != week.isoformat() or parameters.get("regenerate"):
                raise CommandError("Preview differs from operator-confirmed week")
            # Operator explicitly approved this fixture week via --confirmed-week.
            invoke({**parameters, "confirm_token": preview["confirm_token"]})
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            job = SautaiMealPlanJob.objects.filter(tenant=tenant, week_start=week).first()
            if job and job.status == "ready":
                if job.addressed_by != "linked_id" or not job.result or job.error:
                    raise CommandError("Ready job did not satisfy sim result/identity/error checks")
                self.stdout.write(
                    json.dumps(
                        {
                            "proof": "sautai",
                            "plugin_invoked": True,
                            "job_created": True,
                            "status": "ready",
                            "linked_identity": job.addressed_by == "linked_id",
                            "result_present": bool(job.result),
                            "error_empty": not bool(job.error),
                        }
                    )
                )
                return
            if job and job.status == "failed":
                raise CommandError("Local sim job failed (raw errors deliberately withheld)")
            time.sleep(2)
        raise CommandError("Local sim job deadline exceeded")
