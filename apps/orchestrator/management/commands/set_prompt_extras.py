"""Set or clear per-tenant prompt extras on ``User.preferences``.

Prompt extras are canary-scoped additions to the base workspace files (e.g.,
``AGENTS.md``) stored as free-form strings under
``User.preferences['prompt_extras'][<section>]``. They are concatenated to
the base content by ``apps.orchestrator.personas.render_workspace_files``.

Known sections: ``agents_md``.

Usage:

    python manage.py set_prompt_extras \\
        --tenant-id 148ccf1c-ef13-47f8-a... \\
        --section agents_md \\
        --file /path/to/rule.md

    python manage.py set_prompt_extras \\
        --tenant-id 148ccf1c-ef13-47f8-a... \\
        --section agents_md \\
        --clear

After setting, trigger a config push for the tenant so the new workspace
file lands on Azure File Share:

    python manage.py force_apply_configs --tenant-id <uuid>

Until that runs, the running container keeps the previous AGENTS.md.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Tenant

_KNOWN_SECTIONS = {"agents_md"}


class Command(BaseCommand):
    help = "Set or clear per-tenant prompt extras (User.preferences['prompt_extras'])"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            required=True,
            help="Tenant UUID (matches apps.tenants.Tenant.id)",
        )
        parser.add_argument(
            "--section",
            required=True,
            choices=sorted(_KNOWN_SECTIONS),
            help="Which base file the extras attach to",
        )
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--file",
            help="Path to a file whose contents become the extras value",
        )
        group.add_argument(
            "--stdin",
            action="store_true",
            help="Read extras value from stdin",
        )
        group.add_argument(
            "--clear",
            action="store_true",
            help="Remove extras for this section",
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        section = options["section"]

        try:
            tenant = Tenant.objects.select_related("user").get(id=tenant_id)
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Tenant {tenant_id!r} not found") from exc

        user = tenant.user
        prefs = dict(user.preferences or {})
        extras_map = dict(prefs.get("prompt_extras") or {})

        if options["clear"]:
            if section in extras_map:
                del extras_map[section]
                action = f"cleared extras for section {section!r}"
            else:
                action = f"no extras set for section {section!r} (noop)"
        else:
            if options["file"]:
                with open(options["file"]) as fh:
                    value = fh.read()
            else:
                value = sys.stdin.read()
            value = value.strip()
            if not value:
                raise CommandError("Empty extras value; refusing to write")
            extras_map[section] = value
            action = f"set extras for section {section!r} ({len(value)} chars)"

        if extras_map:
            prefs["prompt_extras"] = extras_map
        else:
            prefs.pop("prompt_extras", None)

        user.preferences = prefs
        user.save(update_fields=["preferences"])

        self.stdout.write(self.style.SUCCESS(f"tenant={tenant_id}: {action}"))
        self.stdout.write(
            "Next: run `python manage.py force_apply_configs --tenant-id "
            f"{tenant_id}` to push the updated workspace files."
        )
