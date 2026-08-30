"""Validate and set the server-owned config for the tenant site editor."""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant

_MANIFEST = (
    Path(__file__).resolve().parents[4]
    / "runtime"
    / "openclaw"
    / "plugins"
    / "nbhd-site-editor"
    / "openclaw.plugin.json"
)
_OWNER_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")


def _validated_config(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise CommandError("config must be a JSON object")
    schema = json.loads(_MANIFEST.read_text())["configSchema"]
    properties = schema["properties"]
    unknown = sorted(set(raw) - set(properties))
    if unknown:
        raise CommandError(f"unknown config keys: {', '.join(unknown)}")

    for key, value in raw.items():
        expected = properties[key]["type"]
        valid = (
            (expected == "string" and isinstance(value, str))
            or (expected == "array" and isinstance(value, list) and all(isinstance(item, str) for item in value))
            or (expected == "integer" and type(value) is int and value >= properties[key].get("minimum", 0))
        )
        if not valid:
            raise CommandError(f"invalid value for {key}: expected {expected}")
        if expected == "string" and len(value) > properties[key].get("maxLength", len(value)):
            raise CommandError(f"invalid value for {key}: too long")
        if key in ("owner", "repo") and not _OWNER_REPO_PATTERN.fullmatch(value):
            raise CommandError(f"invalid value for {key}: unsupported characters")
        if key == "branch" and (not _BRANCH_PATTERN.fullmatch(value) or ".." in value):
            raise CommandError("invalid value for branch: unsupported characters")
    return raw


class Command(BaseCommand):
    help = "Set validated per-tenant nbhd-site-editor config and optionally flip its manifest-ready flag"

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True, help="Tenant UUID")
        parser.add_argument("--file", required=True, help="Path to a JSON config file")
        state = parser.add_mutually_exclusive_group()
        state.add_argument("--enable", action="store_true", help="Enable the site editor")
        state.add_argument("--disable", action="store_true", help="Disable the site editor")

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(id=options["tenant_id"])
        except Tenant.DoesNotExist as exc:
            raise CommandError("tenant not found") from exc

        try:
            config = _validated_config(json.loads(Path(options["file"]).read_text()))
        except CommandError:
            raise
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise CommandError(str(exc).replace("\n", " ")) from exc

        tenant.site_editor_config = config
        update_fields = ["site_editor_config"]
        if options["enable"] or options["disable"]:
            tenant.site_editor_enabled = bool(options["enable"])
            update_fields.append("site_editor_enabled")
        tenant.save(update_fields=update_fields)
        tenant.bump_pending_config()
        keys = ",".join(sorted(config))
        if options["enable"]:
            self.stdout.write(
                self.style.WARNING(
                    "WARNING: the running image must already contain "
                    "/opt/nbhd/plugins/nbhd-site-editor — enable only after the image roll"
                )
            )
        self.stdout.write(f"site_editor_enabled={tenant.site_editor_enabled} keys={keys}")
