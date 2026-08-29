"""Render an OpenClaw config for CI smoke-testing (default or maximal flags).

The ``--maximal`` variant turns on ``friends_enabled`` plus every experimental
feature gate, so the generated ``openclaw.json`` exercises the whole surface:
the friends plugin path in ``plugins.load.paths`` (the 2026-07-05 boot-crash
class — a declared plugin the image must actually contain) AND the version-gated
``agents.defaults`` branches (params / contextPruning / built-in heartbeat).

CI generates this in the Postgres-backed smoke job and hands it to the freshly
built OpenClaw image (``openclaw doctor``) so the binary validates it against the
real ``/opt/nbhd/plugins/`` tree — catching a missing plugin or a schema-invalid
block before deploy.

Never persists: the throwaway tenant is created inside a rolled-back transaction.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone


class Command(BaseCommand):
    help = "Render an OpenClaw smoke config (default, or --maximal feature flags)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--maximal",
            action="store_true",
            help="Enable friends + every experimental flag (widest config surface).",
        )
        parser.add_argument(
            "--telegram-chat-id",
            type=int,
            default=999042,
            help="Synthetic Telegram chat id for the throwaway tenant.",
        )
        parser.add_argument(
            "--output",
            default="",
            help="Write config JSON to this path (default: stdout).",
        )

    def handle(self, *args, **options):
        from apps.orchestrator.config_generator import config_to_json, generate_openclaw_config
        from apps.orchestrator.tool_policy import OPENCLAW_CURRENT_VERSION
        from apps.tenants.services import create_tenant

        with transaction.atomic():
            tenant = create_tenant(
                display_name="SmokeConfig",
                telegram_chat_id=options["telegram_chat_id"],
            )
            if options["maximal"]:
                tenant.openclaw_version = OPENCLAW_CURRENT_VERSION
                tenant.friends_enabled = True
                tenant.friends_agent_propose_enabled = True
                tenant.experimental_built_in_heartbeat = True
                tenant.experimental_typed_journal_lifecycle = True
                tenant.experimental_memory_core_enabled = True
                tenant.experimental_active_memory_enabled = True
                tenant.experimental_dreaming_enabled = True
                tenant.experimental_typed_crons = True
                tenant.fuel_enabled = True
                tenant.site_publishing_enabled = True
                tenant.site_editor_enabled = True
                tenant.site_editor_config = {
                    "owner": "smoke-owner",
                    "repo": "smoke-repo",
                    "branch": "main",
                    "allowPaths": ["web/src/pages/*.js", "web/public/index.html"],
                    "denyPaths": [".github/**"],
                    "maxTextBytes": 262144,
                    "maxImageBytes": 2097152,
                    "maxFiles": 20,
                    "maxTotalBytes": 5242880,
                    "deployMinutes": 6,
                    "authorEmail": "nbhd-site-editor@users.noreply.github.com",
                }
                tenant.journal_shaping_enabled = True
                tenant.document_ingestion_enabled = True
                tenant.datebook_manifest_ok = True
                tenant.datebook_enabled = True
                tenant.datebook_events_consent_at = timezone.now()
                tenant.save()

            if options["maximal"]:
                # Exercise the canary-only bridge path, conversation-hook
                # policies, and both reporter metering scopes in image doctor.
                with override_settings(
                    SUBAGENT_TENANT_IDS=str(tenant.id),
                    USAGE_HOOKS_TENANT_IDS=str(tenant.id),
                ):
                    config_json = config_to_json(generate_openclaw_config(tenant))
            else:
                config_json = config_to_json(generate_openclaw_config(tenant))

            # Never leave the smoke tenant behind, even against a real DB.
            transaction.set_rollback(True)

        if options["output"]:
            Path(options["output"]).write_text(config_json)
            self.stderr.write(f"Wrote smoke config to {options['output']}")
        else:
            self.stdout.write(config_json)
