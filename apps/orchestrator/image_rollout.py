"""Per-tenant allowlist gating the automatic roll of active tenants onto
``settings.OPENCLAW_IMAGE_TAG``.

Why this exists: every merge to ``main`` rebuilds the OpenClaw image from
``Dockerfile.openclaw`` and points the Django app's ``OPENCLAW_IMAGE_TAG`` at
it (see the deploy job in ci-cd.yml). The hourly ``apply_pending_configs`` cron
and ``wake_hibernated_tenant`` then roll every active tenant onto that tag. For
an ordinary same-schema bump that is fine, but for a storage/schema-crossing
image (e.g. the 2026.9.4 runtime, which only boots with the node-owned
workspace mount options + the oc-state retrofit) an unguarded roll bricks the
WHOLE fleet within the hour.

This gate makes the auto-roll opt-in. Default (empty setting) = roll NOBODY, so
a deploy that bumps the tag never moves a tenant on its own. A staged rollout
sets ``OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS`` to a comma-separated list of tenant
UUIDs (canary -> soak -> wider), or ``*`` once the image is fleet-verified.

Explicit operator actions (``rollout-atomic-bump`` with a ``tenant_id``, the
``bump_openclaw_version`` command, provisioning a NEW tenant) are NOT gated by
this — they already name their target. This only guards the fleet-wide *auto*
roll paths.
"""

from __future__ import annotations

from django.conf import settings


def image_rollout_allowed(tenant_id) -> bool:
    """True if the auto-roll paths may move ``tenant_id`` onto OPENCLAW_IMAGE_TAG.

    Empty allowlist -> False for every tenant (safe default). ``*`` -> True for
    all. Otherwise True only for tenant UUIDs listed in
    ``OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS``.
    """
    raw = str(getattr(settings, "OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS", "") or "").strip()
    if not raw:
        return False
    if raw == "*":
        return True
    allowed = {part.strip() for part in raw.split(",") if part.strip()}
    return str(tenant_id) in allowed
