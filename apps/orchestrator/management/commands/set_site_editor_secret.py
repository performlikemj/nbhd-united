"""Write or unbind a tenant's GitHub site-editor token without exposing it."""

from __future__ import annotations

import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.byo_models.services import _write_secret_to_kv
from apps.orchestrator.azure_client import (
    assign_key_vault_role,
    ensure_site_editor_secret,
    get_identity_client,
    is_mock,
    site_editor_kv_secret_name,
)
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

        if options["clear"]:
            try:
                ensure_site_editor_secret(tenant, enabled=False)
            except Exception as exc:
                raise CommandError("failed at reconcile; value not shown") from exc
        else:
            token = sys.stdin.read().strip()
            stage = "validate"
            try:
                if not token:
                    raise ValueError("empty token")
                if "\n" in token or "\r" in token:
                    raise ValueError("multiline token")

                if not tenant.key_vault_prefix:
                    stage = "prefix"
                    tenant.key_vault_prefix = f"tenants-{tenant.user_id}"
                    tenant.save(update_fields=["key_vault_prefix"])
                    self.stdout.write(f"key_vault_prefix={tenant.key_vault_prefix}")

                secret_name = site_editor_kv_secret_name(tenant)
                stage = "write"
                _write_secret_to_kv(secret_name, token, log_label="site-editor")

                stage = "grant"
                if not is_mock():
                    identity_client = get_identity_client()
                    mi_name = tenant.managed_identity_id.rsplit("/", 1)[-1]
                    principal_id = identity_client.user_assigned_identities.get(
                        resource_group_name=settings.AZURE_RESOURCE_GROUP,
                        resource_name=mi_name,
                    ).principal_id
                    assign_key_vault_role(principal_id, secret_names=[secret_name])

                stage = "reconcile"
                ensure_site_editor_secret(tenant, enabled=True, force_revision=True)
            except Exception as exc:
                raise CommandError(f"failed at {stage}; value not shown") from exc

        self.stdout.write("ok")
