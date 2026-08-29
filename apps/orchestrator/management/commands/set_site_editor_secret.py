"""Write or unbind a tenant's GitHub site-editor token without exposing it."""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand, CommandError

from apps.byo_models.services import _write_secret_to_kv
from apps.orchestrator.azure_client import ensure_site_editor_secret
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Set the site-editor GitHub token from stdin, or clear its container binding. "
        "--clear leaves the Key Vault value in place."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True, help="Tenant UUID")
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--from-stdin", action="store_true", help="Read the token from stdin")
        action.add_argument(
            "--clear",
            action="store_true",
            help="Remove the container secret/env binding; the Key Vault value may stay",
        )

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(id=options["tenant_id"])
        except Tenant.DoesNotExist as exc:
            raise CommandError("tenant not found") from exc

        try:
            if options["clear"]:
                ensure_site_editor_secret(tenant, enabled=False)
            else:
                raw = sys.stdin.read()
                token = raw.strip()
                if not token:
                    raise CommandError("token is empty")
                if "\n" in token or "\r" in token:
                    raise CommandError("token must be one line")
                if not tenant.key_vault_prefix:
                    raise CommandError("tenant has no Key Vault prefix")
                secret_name = f"{tenant.key_vault_prefix}-github-site-token"
                _write_secret_to_kv(secret_name, token)
                ensure_site_editor_secret(tenant, enabled=True)
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(str(exc).replace("\n", " ")) from exc

        self.stdout.write("ok")
