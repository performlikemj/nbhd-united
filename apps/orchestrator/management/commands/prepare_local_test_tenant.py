"""Provision the single local tenant after MJ creates its account via signup."""

import hashlib
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.orchestrator.services import update_tenant_config
from apps.tenants.models import Tenant, User


def persona_facts(persona):
    """Read the harness confirmed-only manifest, retaining fact IDs and citations."""
    if persona.get("persona") == "yuki" and persona.get("persona_version") == "3":
        rows = persona.get("facts")
        if (
            isinstance(rows, list)
            and rows
            and all(
                isinstance(row, dict)
                and all(isinstance(row.get(key), str) and row[key].strip() for key in ("id", "citation", "text"))
                for row in rows
            )
        ):
            return [f"{row['id']}: {row['text']} (source: {row['citation']})" for row in rows]
    facts = persona.get("confirmed_facts")
    if persona.get("version") == 3 and isinstance(facts, list) and facts:
        if all(isinstance(fact, str) and fact.strip() for fact in facts):
            return facts
    raise CommandError("Expected harness v3 confirmed facts; refuse invented/default facts")


class Command(BaseCommand):
    help = "Prepare the isolated Yuki synthetic tenant; never creates an account or password"

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--persona-file", help="Harness sim/persona/yuki.json confirmed-only v3 manifest")

    def handle(self, *args, **options):
        if not getattr(settings, "LOCAL_TEST_ROOT", "") or not settings.DEBUG or os.environ.get("AZURE_MOCK") != "true":
            raise CommandError("Requires isolated DEBUG/AZURE_MOCK local test settings")
        if settings.DATABASES["default"]["NAME"] != "nbhd_yuki_test":
            raise CommandError("Refusing any other database")
        try:
            user = User.objects.get(email__iexact=options["email"])
        except User.DoesNotExist:
            raise CommandError(
                "MJ must first create the account at http://127.0.0.1:18080/local-test/signup/"
            ) from None
        tid = os.environ["NBHD_TENANT_ID"]
        if Tenant.objects.exclude(id=tid).exists():
            raise CommandError("Expected only the designated local tenant")
        facts = None
        if options["persona_file"]:
            raw = Path(options["persona_file"]).read_bytes()
            persona = json.loads(raw)
            facts = persona_facts(persona)
            user.preferences = {
                **(user.preferences or {}),
                "local_test_persona_v3": facts,
                "local_test_persona_sha256": hashlib.sha256(raw).hexdigest(),
            }
        user.timezone = "Asia/Tokyo"
        user.preferences = {**(user.preferences or {}), "agent_persona": "neighbor"}
        user.save(update_fields=["timezone", "preferences"])
        tenant, _ = Tenant.objects.get_or_create(
            id=tid,
            defaults={
                "user": user,
                "is_synthetic": True,
                "is_eval_sink": False,
                "model_tier": "starter",
                "monthly_cost_budget": Decimal("1.00"),
                "monthly_token_budget": 100000,
                "is_budget_exempt": False,
                "purchased_credit": 0,
                "is_trial": True,
                "trial_started_at": timezone.now(),
                "trial_ends_at": timezone.now() + timedelta(days=30),
                "openclaw_version": "2026.9.1",
                "sautai_enabled": True,
            },
        )
        if tenant.user_id != user.id or not tenant.is_synthetic or tenant.is_eval_sink:
            raise CommandError("Tenant identity/synthetic gate failed")
        if tenant.status == Tenant.Status.PENDING:
            call_command("provision_tenant", str(tenant.id))
        tenant.refresh_from_db()
        if tenant.status != Tenant.Status.ACTIVE:
            raise CommandError("Provisioning did not reach active")
        tenant.container_fqdn = "127.0.0.1:19443"
        tenant.internal_api_key = settings.NBHD_INTERNAL_API_KEY
        tenant.save(update_fields=["container_fqdn", "internal_api_key", "updated_at"])
        update_tenant_config(str(tenant.id))
        self.stdout.write(
            json.dumps(
                {
                    "status": "prepared",
                    "synthetic": True,
                    "eval_sink": False,
                    "persona_v3_seeded": bool(user.preferences.get("local_test_persona_v3")),
                }
            )
        )
