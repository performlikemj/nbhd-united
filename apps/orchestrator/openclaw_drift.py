"""Compare a tenant's LIVE Azure Container App template against the
code-declared OpenClaw desired state, and (for the fleet cron) turn the
result into ONE consolidated Pushover alert.

Why this exists (MJ, 2026-09-17): the 2026.9.4 runtime only boots when the
container carries the node-owned workspace ``mountOptions``, the ``oc-state``
EmptyDir volume + mount, and the four state-relocation env vars. Those are
applied at provision time and by ``ensure_openclaw_ready``; nothing else
rewrites them. A hand edit on the Azure portal (or a partial revision from a
half-finished script) silently strips them, and the tenant only breaks on its
NEXT restart — hours or days later, with no obvious cause. This module makes
that drift visible: the same constants that ``azure_client`` provisions with
are the comparison baseline, so the code stays the single source of truth.

Two classes of drift are deliberately kept apart:

* **storage / env drift** — always real, always fixable by
  ``ensure_openclaw_ready``, always alerts.
* **image drift** — a tenant not on ``OPENCLAW_IMAGE_TAG`` is EXPECTED during
  a staged rollout (``image_rollout_allowed`` gates the auto-roll), so it is
  reported but never alerts. The one image case that DOES alert is the DB row
  disagreeing with Azure about which tag is running: that only happens when
  someone changed the image outside the code paths.

Everything here is READ-ONLY against Azure and never touches tenant data.
The pure comparison (``compare_storage`` / ``compare_image``) takes the SDK
model object and has no I/O, so it is unit-testable without Azure or a DB.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from django.conf import settings

from apps.orchestrator.azure_client import (
    _OC_STATE_ENV,
    _OC_STATE_PATH,
    _OC_STATE_VOLUME,
    _WORKSPACE_MOUNT_OPTIONS,
    _WORKSPACE_VOLUME,
)

logger = logging.getLogger(__name__)

OPENCLAW_CONTAINER_NAME = "openclaw"

# Field identifiers used in command output, JSON, and the Pushover body.
FIELD_CONTAINER = "container:openclaw"
FIELD_WORKSPACE_MOUNT_OPTIONS = "workspace.mountOptions"
FIELD_OC_STATE_VOLUME = f"volume:{_OC_STATE_VOLUME}"
FIELD_OC_STATE_MOUNT = f"mount:{_OC_STATE_VOLUME}"
FIELD_IMAGE = "image"


def env_field(name: str) -> str:
    return f"env:{name}"


# Image drift kinds — only ``db-mismatch`` alerts.
IMAGE_STALE_GATED = "stale-gated"  # not on OPENCLAW_IMAGE_TAG, tenant NOT allowlisted (expected)
IMAGE_STALE_ALLOWED = "stale-allowed"  # not on OPENCLAW_IMAGE_TAG, allowlisted (auto-roll pending)
IMAGE_DB_MISMATCH = "db-mismatch"  # Tenant.container_image_tag != live tag (hand edit)

# Pushover caps messages at 1024 chars; leave headroom for the title/footer.
_ALERT_MAX_CHARS = 1000
_ALERT_MAX_TENANTS = 10


@dataclass(frozen=True)
class FieldDrift:
    """One desired-state field that disagrees with the live template."""

    field: str
    expected: str
    actual: str
    alert: bool = True
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "alert": self.alert,
            "note": self.note,
        }


@dataclass
class TenantDrift:
    """Drift report for one tenant. ``error`` is set when the live template
    could not be read at all (missing app, auth failure, network) — that is
    itself alert-worthy: a deleted Container App is the worst drift there is.
    """

    tenant_id: str
    container_name: str
    fields: list[FieldDrift] = field(default_factory=list)
    error: str = ""

    @property
    def short_id(self) -> str:
        return self.tenant_id[:8]

    @property
    def alerting_fields(self) -> list[FieldDrift]:
        return [f for f in self.fields if f.alert]

    @property
    def gated_fields(self) -> list[FieldDrift]:
        return [f for f in self.fields if not f.alert]

    @property
    def has_drift(self) -> bool:
        return bool(self.fields) or bool(self.error)

    @property
    def should_alert(self) -> bool:
        return bool(self.alerting_fields) or bool(self.error)

    def as_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "container": self.container_name,
            "error": self.error,
            "fields": [f.as_dict() for f in self.fields],
            "alert": self.should_alert,
        }


@dataclass
class FleetDriftReport:
    checked: int = 0
    tenants: list[TenantDrift] = field(default_factory=list)
    mock: bool = False

    @property
    def drifted(self) -> list[TenantDrift]:
        return [t for t in self.tenants if t.has_drift]

    @property
    def alerting(self) -> list[TenantDrift]:
        return [t for t in self.tenants if t.should_alert]

    @property
    def gated_only(self) -> list[TenantDrift]:
        return [t for t in self.tenants if t.has_drift and not t.should_alert]

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "drifted": len(self.drifted),
            "alerting": len(self.alerting),
            "gated_only": len(self.gated_only),
            "mock": self.mock,
            "tenants": [t.as_dict() for t in self.tenants if t.has_drift],
        }


# ── Pure comparison (no I/O) ─────────────────────────────────────────


def _openclaw_container(app):
    for container in getattr(app.template, "containers", None) or []:
        if getattr(container, "name", None) == OPENCLAW_CONTAINER_NAME:
            return container
    return None


def compare_storage(app) -> list[FieldDrift]:
    """Storage + env drift for one SDK ``ContainerApp`` object.

    Mirrors, field for field, what ``azure_client._ensure_oc_state_dir_in_template``
    would change — so ``ensure_openclaw_ready`` can report per-field and the
    two never disagree (guarded by a test).
    """
    drift: list[FieldDrift] = []
    template = app.template

    container = _openclaw_container(app)
    if container is None:
        drift.append(FieldDrift(FIELD_CONTAINER, "present", "missing", note="no 'openclaw' container in template"))
        return drift

    # 1. Workspace AzureFile mount options (9.4 fs-safe needs node-owned 0700).
    workspace = None
    for volume in getattr(template, "volumes", None) or []:
        if getattr(volume, "name", None) == _WORKSPACE_VOLUME and getattr(volume, "storage_type", None) == "AzureFile":
            workspace = volume
            break
    if workspace is None:
        drift.append(
            FieldDrift(
                FIELD_WORKSPACE_MOUNT_OPTIONS,
                _WORKSPACE_MOUNT_OPTIONS,
                "<no 'workspace' AzureFile volume>",
                note="not auto-fixable: the workspace share volume itself is missing",
            )
        )
    else:
        actual = getattr(workspace, "mount_options", None)
        if actual != _WORKSPACE_MOUNT_OPTIONS:
            drift.append(FieldDrift(FIELD_WORKSPACE_MOUNT_OPTIONS, _WORKSPACE_MOUNT_OPTIONS, str(actual)))

    # 2. oc-state EmptyDir volume + mount.
    volume_names = {getattr(v, "name", None) for v in getattr(template, "volumes", None) or []}
    if _OC_STATE_VOLUME not in volume_names:
        drift.append(FieldDrift(FIELD_OC_STATE_VOLUME, "EmptyDir", "missing"))

    mounts = {getattr(m, "volume_name", None): m for m in getattr(container, "volume_mounts", None) or []}
    mount = mounts.get(_OC_STATE_VOLUME)
    if mount is None:
        drift.append(FieldDrift(FIELD_OC_STATE_MOUNT, _OC_STATE_PATH, "missing"))
    elif getattr(mount, "mount_path", None) != _OC_STATE_PATH:
        drift.append(
            FieldDrift(
                FIELD_OC_STATE_MOUNT,
                _OC_STATE_PATH,
                str(getattr(mount, "mount_path", None)),
                note="not auto-fixable: mount exists at a different path",
            )
        )

    # 3. State-relocation env vars.
    env = {getattr(e, "name", None): getattr(e, "value", None) for e in getattr(container, "env", None) or []}
    for name, expected in _OC_STATE_ENV.items():
        if name not in env:
            drift.append(FieldDrift(env_field(name), expected, "missing"))
        elif env[name] != expected:
            drift.append(FieldDrift(env_field(name), expected, str(env[name])))

    return drift


def image_tag_of(image: str | None) -> str:
    """``registry/repo:tag`` → ``tag`` (empty string if untagged)."""
    if not image or ":" not in image.rsplit("/", 1)[-1]:
        return ""
    return image.rsplit(":", 1)[-1]


def compare_image(
    app,
    *,
    desired_tag: str,
    rollout_allowed: bool,
    db_image_tag: str = "",
) -> list[FieldDrift]:
    """Image drift for one template.

    ``desired_tag`` empty / ``latest`` disables the desired-tag check (mirrors
    ``apply_pending_configs``, which never rolls onto ``latest``). The
    desired-tag comparison NEVER alerts — staged rollouts make it expected —
    but it is reported so ``ensure_openclaw_ready`` can act on it when the
    tenant is allowlisted. A DB/live disagreement DOES alert: the code paths
    that change an image always write ``container_image_tag`` alongside.
    """
    container = _openclaw_container(app)
    if container is None:
        return []  # compare_storage already reports the missing container
    live_tag = image_tag_of(getattr(container, "image", None))
    drift: list[FieldDrift] = []

    if db_image_tag and live_tag and db_image_tag != live_tag:
        drift.append(
            FieldDrift(
                FIELD_IMAGE,
                db_image_tag,
                live_tag,
                alert=True,
                note=f"{IMAGE_DB_MISMATCH}: Tenant.container_image_tag disagrees with the live template",
            )
        )
        # A DB mismatch supersedes the stale check — the live tag is not
        # something our code put there, so "stale vs desired" is moot.
        return drift

    if desired_tag and desired_tag != "latest" and live_tag != desired_tag:
        kind = IMAGE_STALE_ALLOWED if rollout_allowed else IMAGE_STALE_GATED
        drift.append(
            FieldDrift(
                FIELD_IMAGE,
                desired_tag,
                live_tag or "<untagged>",
                alert=False,
                note=kind,
            )
        )
    return drift


def compare_template(app, *, tenant_id: str, db_image_tag: str = "") -> list[FieldDrift]:
    """Storage + env + image drift for one tenant's live template."""
    from apps.orchestrator.image_rollout import image_rollout_allowed

    desired_tag = str(getattr(settings, "OPENCLAW_IMAGE_TAG", "") or "")
    return compare_storage(app) + compare_image(
        app,
        desired_tag=desired_tag,
        rollout_allowed=image_rollout_allowed(tenant_id),
        db_image_tag=db_image_tag or "",
    )


# ── Fleet check (Azure reads only) ───────────────────────────────────


def active_tenant_queryset():
    """The reconcile/drift population: active, provisioned, NOT hibernated.

    Hibernated tenants are skipped so a reconcile never wakes one (a template
    write activates a revision); they get reconciled on their next wake by the
    image-update path.
    """
    from apps.tenants.models import Tenant

    return Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
        hibernated_at__isnull=True,
    ).order_by("created_at")


def check_tenant_drift(tenant) -> TenantDrift:
    """Read one tenant's live template and compare. Never raises — an Azure
    error becomes ``TenantDrift.error`` so one bad tenant can't hide the rest.
    """
    from apps.orchestrator.azure_client import get_container_client

    result = TenantDrift(tenant_id=str(tenant.id), container_name=tenant.container_id)
    try:
        client = get_container_client()
        app = client.container_apps.get(settings.AZURE_RESOURCE_GROUP, tenant.container_id)
        result.fields = compare_template(
            app,
            tenant_id=str(tenant.id),
            db_image_tag=tenant.container_image_tag or "",
        )
    except Exception as exc:  # noqa: BLE001 — one tenant's failure must not abort the sweep
        # Exception class + first line only: SDK errors can embed request
        # bodies, and this string ends up in a Pushover message.
        first_line = str(exc).splitlines()[0] if str(exc) else ""
        result.error = f"{type(exc).__name__}: {first_line[:120]}"
        logger.warning(
            "OpenClaw drift check failed for %s (%s): %s", tenant.container_id, result.short_id, result.error
        )
    return result


def check_fleet_drift(tenants: Iterable) -> FleetDriftReport:
    """READ-ONLY drift sweep over ``tenants``. Under ``AZURE_MOCK`` returns an
    empty report flagged ``mock`` — never fabricates drift.
    """
    from apps.orchestrator.azure_client import _is_mock

    report = FleetDriftReport()
    if _is_mock():
        report.mock = True
        return report
    for tenant in tenants:
        report.checked += 1
        report.tenants.append(check_tenant_drift(tenant))
    return report


# ── Alerting ─────────────────────────────────────────────────────────


def format_alert(report: FleetDriftReport) -> str:
    """One consolidated, size-capped Pushover body. Field NAMES only — never
    env values or SDK error bodies — so nothing secret can ride along.
    """
    alerting = report.alerting
    lines = [f"OpenClaw fleet drift — {len(alerting)}/{report.checked} tenant(s) drifted from code:"]
    for t in alerting[:_ALERT_MAX_TENANTS]:
        if t.error:
            detail = f"unreadable ({t.error.split(':', 1)[0]})"
        else:
            detail = ", ".join(f.field for f in t.alerting_fields)
        lines.append(f"  - {t.container_name} ({t.short_id}): {detail}")
    if len(alerting) > _ALERT_MAX_TENANTS:
        lines.append(f"  ... and {len(alerting) - _ALERT_MAX_TENANTS} more")
    lines.append("Fix: manage.py ensure_openclaw_ready --all (see docs/agents/openclaw-fleet-admin.md)")

    body = "\n".join(lines)
    if len(body) > _ALERT_MAX_CHARS:
        body = body[: _ALERT_MAX_CHARS - 1] + "…"
    return body


def send_drift_alert(report: FleetDriftReport) -> str:
    """Send the consolidated alert if anything alert-worthy drifted.

    Returns the ``_send_alert_via_pushover`` delivery status, or ``"skipped"``
    when there is nothing to alert on.
    """
    if not report.alerting:
        return "skipped"
    from apps.cron.views import _send_alert_via_pushover

    return _send_alert_via_pushover(format_alert(report))
