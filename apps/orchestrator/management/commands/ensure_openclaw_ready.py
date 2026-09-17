"""THE one button: reconcile tenant Container Apps to the code-declared
OpenClaw desired state. Idempotent — a second run is a clean no-op.

The code (``apps/orchestrator/azure_client.py``) is the source of truth for
what an OpenClaw container must look like; Azure is a cache of it. A hand edit
on the portal, a half-applied script, or a revision created by an older code
path leaves a tenant drifted — and a drifted 2026.9.4 container only fails on
its NEXT restart. Instead of remembering which ad-hoc script fixes which field,
run this. It reads each tenant's LIVE template, reports every field that
disagrees, and rewrites only what differs:

  * ``workspace.mountOptions``  — node-owned 0700 CIFS options (9.4 fs-safe)
  * ``volume:oc-state`` / ``mount:oc-state`` — EmptyDir for runtime SQLite
  * ``env:OPENCLAW_STATE_DIR`` + 3 siblings — state relocation env vars
  * ``image`` — ONLY when the tenant is allowlisted by
    ``OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS``; otherwise reported as
    ``stale-gated`` and left alone. Allowlisted rolls go through the same
    ``apply_single_tenant_image_task`` the hourly cron uses (cron snapshot →
    image → version → config → cron restore).

Storage/env fields are reconciled regardless of the image gate. Note that the
2026.5.28 runtime also honors the relocation env vars, so on a 5.28 tenant
this moves its runtime state (sessions, cron store, identity) to ephemeral
disk exactly as it does on 9.4 — durable truth stays on the share + Postgres.

Every write is a new revision = a container restart, so — like the hourly
cron — a tenant with a cron mid-flight or imminent is DEFERRED, and a tenant
whose image is about to roll gets ONE restart (the image bump bakes the
storage state in) rather than two. Hibernated tenants are never written to
(a template write would wake them).

Usage:

    python manage.py ensure_openclaw_ready --tenant <uuid>       # one tenant
    python manage.py ensure_openclaw_ready --all                 # active, non-hibernated
    python manage.py ensure_openclaw_ready --all --dry-run       # report drift, write nothing
    python manage.py ensure_openclaw_ready --all --no-image      # storage/env only

Supersedes ``ensure_oc_state_dir_mount`` (kept for backwards compatibility).
Full runbook: docs/agents/openclaw-fleet-admin.md
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.azure_client import ensure_openclaw_storage_ready, is_mock
from apps.orchestrator.image_rollout import image_rollout_allowed
from apps.orchestrator.openclaw_drift import (
    FIELD_IMAGE,
    IMAGE_DB_MISMATCH,
    active_tenant_queryset,
    compare_image,
)
from apps.tenants.models import Tenant

PLAN_OK = "ok"
PLAN_MISMATCH = "mismatch"
PLAN_GATED = "gated"
PLAN_ROLL = "roll"


@dataclass
class ImagePlan:
    plan: str
    line: str
    desired_tag: str = ""


class Command(BaseCommand):
    help = "Reconcile tenant Container Apps to the code-declared OpenClaw storage/env/image state (idempotent)."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--tenant", help="Single tenant UUID")
        target.add_argument("--all", action="store_true", help="All active, non-hibernated tenants")
        parser.add_argument("--dry-run", action="store_true", help="Report per-field drift without writing")
        parser.add_argument(
            "--no-image",
            action="store_true",
            help="Reconcile storage/env only; never roll the image even if the tenant is allowlisted",
        )

    # ── helpers ──────────────────────────────────────────────────────

    def _tenants(self, options) -> list[Tenant]:
        if options["tenant"]:
            tenant = Tenant.objects.filter(id=options["tenant"], container_id__gt="").first()
            if tenant is None:
                raise CommandError(f"No provisioned tenant with id {options['tenant']}")
            if tenant.hibernated_at:
                # A template write activates a revision = wakes the tenant while
                # hibernated_at stays set. Never do that from here.
                raise CommandError(
                    f"Tenant {str(tenant.id)[:8]} is hibernated; a template write would wake it. "
                    "It converges on its next wake — or wake it first, then re-run."
                )
            return [tenant]
        return list(active_tenant_queryset())

    def _image_plan(self, tenant: Tenant, *, skip_image: bool) -> ImagePlan:
        """Decide what (if anything) to do about the image field. Read-only."""
        if skip_image:
            return ImagePlan(PLAN_OK, "image: skipped (--no-image)")

        from apps.orchestrator.azure_client import get_container_client

        client = get_container_client()
        app = client.container_apps.get(settings.AZURE_RESOURCE_GROUP, tenant.container_id)
        desired_tag = str(getattr(settings, "OPENCLAW_IMAGE_TAG", "") or "")
        drifts = compare_image(
            app,
            desired_tag=desired_tag,
            rollout_allowed=image_rollout_allowed(tenant.id),
            db_image_tag=tenant.container_image_tag or "",
        )
        drift = next((d for d in drifts if d.field == FIELD_IMAGE), None)
        if drift is None:
            return ImagePlan(PLAN_OK, "image: ok")
        if IMAGE_DB_MISMATCH in drift.note:
            return ImagePlan(
                PLAN_MISMATCH,
                f"image: DB/LIVE MISMATCH — Tenant.container_image_tag={drift.expected} but Azure runs "
                f"{drift.actual}. Not auto-fixed: decide which is right (bump_openclaw_version to re-pin).",
            )
        if not image_rollout_allowed(tenant.id):
            return ImagePlan(
                PLAN_GATED,
                f"image: stale-gated (live {drift.actual}, desired {desired_tag}) — tenant not in "
                "OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS; left alone",
            )
        return ImagePlan(PLAN_ROLL, f"image: stale-allowed — roll {drift.actual} → {desired_tag}", desired_tag)

    def _roll_image(self, tenant: Tenant, desired_tag: str) -> tuple[bool, str]:
        """Roll via the cron's task (snapshot → image → version → config →
        cron restore). Returns (ok, status line)."""
        from apps.orchestrator.tasks import apply_single_tenant_image_task

        apply_single_tenant_image_task(str(tenant.id), desired_tag)
        refreshed = Tenant.objects.filter(id=tenant.id).values_list("container_image_tag", flat=True).first()
        if refreshed == desired_tag:
            return True, f"image: ROLLED → {desired_tag} (storage/env baked into the same revision)"
        return False, f"image: roll FAILED — container_image_tag is still {refreshed!r}; see logs"

    # ── main ─────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        skip_image = options["no_image"]

        if is_mock():
            self.stdout.write("[MOCK] AZURE_MOCK=true — nothing to reconcile.")
            return

        tenants = self._tenants(options)
        if not tenants:
            self.stdout.write("No eligible tenants found.")
            return

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(f"{prefix}Reconciling OpenClaw desired state on {len(tenants)} tenant(s)")

        counts = dict(in_sync=0, fixed=0, unfixable=0, deferred=0, failed=0)
        for tenant in tenants:
            label = f"{tenant.container_id} ({str(tenant.id)[:8]})"
            try:
                self._reconcile_one(tenant, label, dry_run=dry_run, skip_image=skip_image, counts=counts)
            except Exception as exc:  # noqa: BLE001 — keep sweeping; report per tenant
                counts["failed"] += 1
                self.stderr.write(self.style.ERROR(f"  {label}: FAILED — {exc}"))

        verb = "to fix" if dry_run else "fixed"
        self.stdout.write(
            f"{prefix}Done: {counts['in_sync']} in sync, {counts['fixed']} {verb}, "
            f"{counts['unfixable']} with unfixable fields, {counts['deferred']} deferred, {counts['failed']} failed"
        )

    def _reconcile_one(self, tenant: Tenant, label: str, *, dry_run: bool, skip_image: bool, counts: dict) -> None:
        # 1. Read-only probe of the storage/env fields (one GET, no write).
        probe = ensure_openclaw_storage_ready(tenant.container_id, dry_run=True)
        if probe.in_sync:
            self.stdout.write(f"  {label}: storage/env in sync")
        else:
            for d in probe.drift_before:
                tag = "UNFIXABLE" if d.field in probe.unfixable_fields else "drift"
                note = f" ({d.note})" if d.note else ""
                self.stdout.write(f"    {tag}: {d.field}  expected={d.expected}  actual={d.actual}{note}")

        # 2. Decide the image action (read-only).
        image = self._image_plan(tenant, skip_image=skip_image)
        storage_write_needed = bool(probe.fixed_fields)
        will_roll = image.plan == PLAN_ROLL

        if not storage_write_needed and not will_roll:
            if probe.in_sync:
                counts["in_sync"] += 1
            else:
                counts["unfixable"] += 1
                self.stdout.write(self.style.WARNING(f"  {label}: {len(probe.unfixable_fields)} field(s) UNFIXABLE"))
            self.stdout.write(f"    {image.line}")
            return

        if dry_run:
            counts["fixed"] += 1
            if storage_write_needed:
                self.stdout.write(f"  {label}: would fix {len(probe.fixed_fields)} storage/env field(s)")
            self.stdout.write(f"    {image.line}")
            return

        # 3. Any write below restarts the container — defer exactly like the
        #    hourly cron does when a tenant cron is mid-flight or imminent.
        from apps.orchestrator.hibernation import _cron_active_or_imminent

        defer_reason = _cron_active_or_imminent(tenant)
        if defer_reason:
            counts["deferred"] += 1
            self.stdout.write(self.style.WARNING(f"  {label}: DEFERRED ({defer_reason}) — re-run later"))
            return

        # 4. Write. An image roll bakes the storage state into the same
        #    revision (update_container_image → _ensure_oc_state_dir_in_template),
        #    so it is ONE restart, not two.
        if will_roll:
            ok, line = self._roll_image(tenant, image.desired_tag)
            if ok:
                counts["fixed"] += 1
                self.stdout.write(self.style.SUCCESS(f"  {label}: {line}"))
            else:
                counts["failed"] += 1
                self.stderr.write(self.style.ERROR(f"  {label}: {line}"))
            return

        result = ensure_openclaw_storage_ready(tenant.container_id)
        if result.unfixable_fields:
            counts["unfixable"] += 1
        else:
            counts["fixed"] += 1
        status = "revision created" if result.revision_created else "no revision needed"
        self.stdout.write(self.style.SUCCESS(f"  {label}: fixed {len(result.fixed_fields)} field(s) — {status}"))
        self.stdout.write(f"    {image.line}")
