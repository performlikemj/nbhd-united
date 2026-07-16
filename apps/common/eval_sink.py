"""Authoritative real-transport suppression for eval-sink tenants.

Every tenant-aware APNs, Telegram, and LINE egress chokepoint must consult
``suppresses_real_transport`` before invoking its transport client.  APNs is
centralized in ``apps.router.push_views._push_to_user_devices`` (plus the
self-service push-test endpoint); tenant-less Telegram/LINE helpers are guarded
at their tenant-aware call sites.
"""

from __future__ import annotations


def suppresses_real_transport(tenant) -> bool:
    """Return whether ``tenant`` is an internal evidence-only eval sink."""
    return bool(getattr(tenant, "is_eval_sink", False))
