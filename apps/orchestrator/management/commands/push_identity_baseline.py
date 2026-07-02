"""Re-assert the managed SOUL.md / IDENTITY.md baseline to tenant file shares.

Sibling of ``refresh_persona_agents_md``, but for the sentinel-split identity
files. Each push renders the current managed region (persona baseline + any
per-tenant ``prompt_extras``), reads the share copy, and writes back with the
managed region replaced and the agent's growth region (below the END marker)
preserved verbatim — via ``services.reassert_identity_files`` (fail-closed on
read error, no-op when already current).

``--dry-run`` renders the managed region, reads the share, and prints a
managed-only diff summary plus how many chars of agent growth would be
preserved — writing NOTHING. Use it before a fleet push to confirm no growth
region is at risk.

Usage:

    python manage.py push_identity_baseline --file all                 # all active
    python manage.py push_identity_baseline --file soul --tenant <uuid>
    python manage.py push_identity_baseline --file identity --dry-run
    python manage.py push_identity_baseline --file all --include-hibernated
"""

from __future__ import annotations

import concurrent.futures
import logging

from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.azure_client import download_workspace_file
from apps.orchestrator.identity_merge import (
    IDENTITY_BEGIN_MARKER,
    IDENTITY_END_MARKER,
    SOUL_BEGIN_MARKER,
    SOUL_END_MARKER,
    is_known_platform_identity,
    is_known_platform_soul,
)
from apps.orchestrator.personas import render_workspace_files
from apps.orchestrator.services import reassert_identity_files
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

_DEFAULT_MAX_WORKERS = 5

# key -> (env_key, share_path, begin_marker, end_marker, is_legacy_platform)
_SPECS = {
    "soul": ("NBHD_SOUL_MD", "workspace/SOUL.md", SOUL_BEGIN_MARKER, SOUL_END_MARKER, is_known_platform_soul),
    "identity": (
        "NBHD_IDENTITY_MD",
        "workspace/IDENTITY.md",
        IDENTITY_BEGIN_MARKER,
        IDENTITY_END_MARKER,
        is_known_platform_identity,
    ),
}


def _normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


def _managed_region(text: str, begin_marker: str, end_marker: str) -> str | None:
    """Extract the managed region (markers inclusive) from ``text`` or None."""
    if not text:
        return None
    b = text.find(begin_marker)
    if b < 0:
        return None
    e = text.find(end_marker, b + len(begin_marker))
    if e < 0:
        return None
    return text[b : e + len(end_marker)]


def _growth_len(text: str, begin_marker: str, end_marker: str, is_legacy) -> int:
    """How many chars of agent growth ``text`` carries (mirrors splice cases)."""
    if not text or not text.strip():
        return 0
    b = text.find(begin_marker)
    e = text.find(end_marker, b + len(begin_marker)) if b >= 0 else -1
    if b >= 0 and e > b:
        end_line_end = text.find("\n", e + len(end_marker))
        end_line_end = len(text) if end_line_end < 0 else end_line_end + 1
        return len(text[end_line_end:].strip())
    # No markers: a known legacy platform render is replaced (0 growth); anything
    # else is preserved verbatim as growth.
    if is_legacy(text):
        return 0
    return len(text.strip())


class Command(BaseCommand):
    help = "Re-assert managed SOUL.md/IDENTITY.md baseline to tenant file shares (growth preserved)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            choices=["soul", "identity", "all"],
            default="all",
            help="Which file(s) to push (default: all)",
        )
        parser.add_argument("--tenant", default=None, help="Single tenant UUID (default: every active tenant)")
        parser.add_argument(
            "--include-hibernated",
            action="store_true",
            help="Also push hibernated tenants (default: skip — they pick up on next wake)",
        )
        parser.add_argument("--dry-run", action="store_true", help="Show a diff summary, write nothing")
        parser.add_argument("--max-workers", type=int, default=_DEFAULT_MAX_WORKERS, help="Max concurrent uploads")

    def _selected_files(self, which: str) -> tuple[str, ...]:
        return ("soul", "identity") if which == "all" else (which,)

    def handle(self, *args, **options):
        which = options["file"]
        include_hibernated = options["include_hibernated"]
        dry_run = options["dry_run"]
        max_workers = max(1, options["max_workers"])
        files = self._selected_files(which)

        if options["tenant"]:
            qs = Tenant.objects.filter(id=options["tenant"])
        else:
            qs = Tenant.objects.filter(status=Tenant.Status.ACTIVE, container_id__gt="")
            if not include_hibernated:
                qs = qs.filter(hibernated_at__isnull=True)

        tenants = list(qs.select_related("user"))
        if not tenants:
            raise CommandError("No matching tenants found")

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            f"{prefix}push_identity_baseline file={which} for {len(tenants)} tenant(s) (concurrency: {max_workers})"
        )

        if dry_run:
            for tenant in tenants:
                self._describe(tenant, files)
            return

        succeeded = failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(reassert_identity_files, tenant, files=files): tenant for tenant in tenants}
            for future in concurrent.futures.as_completed(futures):
                tenant = futures[future]
                tid = str(tenant.id)[:8]
                try:
                    result = future.result()
                    succeeded += 1
                    changed = ", ".join(k for k, v in result.items() if v) or "no change"
                    self.stdout.write(
                        self.style.SUCCESS(f"  {tenant.container_id or '(no container)'} ({tid}): {changed}")
                    )
                except Exception as exc:
                    failed += 1
                    self.stderr.write(
                        self.style.ERROR(f"  {tenant.container_id or '(no container)'} ({tid}): FAILED — {exc}")
                    )

        self.stdout.write(f"Done: {succeeded} pushed, {failed} failed")
        if failed:
            raise CommandError(f"{failed} tenant(s) failed to push — see errors above")

    def _describe(self, tenant: Tenant, files: tuple[str, ...]) -> None:
        """Dry-run: managed-region diff + preserved-growth char count. No writes."""
        tid = str(tenant.id)[:8]
        persona_key = (tenant.user.preferences or {}).get("agent_persona", "neighbor")
        rendered = render_workspace_files(persona_key, tenant=tenant)
        for key in files:
            env_key, share_path, begin_marker, end_marker, is_legacy = _SPECS[key]
            managed_new = rendered.get(env_key, "")
            try:
                current = download_workspace_file(str(tenant.id), share_path)
            except Exception as exc:
                self.stdout.write(f"  {tid} {key}: read FAILED ({exc}) — would SKIP (fail-closed)")
                continue
            current_managed = _managed_region(current or "", begin_marker, end_marker)
            new_norm = _normalize_ws(_managed_region(managed_new, begin_marker, end_marker) or managed_new)
            if current_managed is None:
                if current and current.strip() and not is_legacy(current):
                    managed_state = "no managed region yet → would PREPEND (case 3, growth kept)"
                else:
                    managed_state = "fresh/legacy → would SEED managed + growth space (case 1)"
            elif _normalize_ws(current_managed) == new_norm:
                managed_state = "managed region: unchanged"
            else:
                managed_state = f"managed region: CHANGED ({len(current_managed)}c → {len(managed_new)}c)"
            growth = _growth_len(current or "", begin_marker, end_marker, is_legacy)
            self.stdout.write(f"  {tid} {key}: {managed_state}; growth preserved: {growth} chars")
