"""Weekly Mission digest (design §7) — the neutral crew coordinator.

A QStash weekly job fans out ONE warm, non-shaming digest per (mission, member,
window). Idempotency is a compare-and-set on ``SharedGoalMembership
.last_digest_window`` (per (member, iso-week)) so nobody is double-nudged even
if the cron re-runs. Each member is reached through their OWN send-to-user seam
(``resolve_user_channel``), so it is clearly the PLATFORM speaking — the agent
never posts into any chat. Quiet hours / caps are whatever the send-to-user path
already enforces; we add none.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from .models import SharedGoalMembership

logger = logging.getLogger(__name__)


def iso_week(now) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def digest_dedup_id(mission_id, tenant_id, window: str) -> str:
    """AutomationRun-style idempotency key for a per-member fan-out. Uses '-'
    separators only — QStash rejects ':' / whitespace in a dedup id (invariant)."""
    return f"mission-{mission_id}-{tenant_id}-{window}"


def run_weekly_mission_digest(now=None) -> dict:
    now = now or timezone.now()
    window = iso_week(now)
    sent = 0
    memberships = SharedGoalMembership.objects.filter(status="active", shared_goal__status="active").select_related(
        "shared_goal", "tenant", "tenant__user"
    )
    for membership in memberships:
        # Compare-and-set: claim this (member, window) exactly once (rowcount 1).
        claimed = (
            SharedGoalMembership.objects.filter(id=membership.id)
            .exclude(last_digest_window=window)
            .update(last_digest_window=window)
        )
        if not claimed:
            continue
        try:
            _deliver_digest(membership.tenant, membership.shared_goal)
            sent += 1
        except Exception:  # noqa: BLE001 — one member's failure must not stop the fan-out
            logger.warning("mission digest delivery failed for member %s", str(membership.tenant_id)[:8], exc_info=True)
    return {"window": window, "sent": sent}


def _deliver_digest(tenant, mission) -> None:
    from . import projection

    text = _render_digest(projection.build_mission_status(mission))
    _deliver_text(tenant, text)


def _render_digest(status: dict) -> str:
    """Warm, non-shaming (§7 tone): '🌱 July Steps — you + @aya both hit 6/7.
    @kiho's had a quieter week — a wave might help.'"""
    lines = [f"\U0001f331 {status['title']} — your crew this week:"]
    for member in status["members"]:
        who = f"@{member['handle']}" if member["handle"] else "a neighbor"
        streak = f", {member['streak']}-day streak" if member["streak"] else ""
        lines.append(f"• {who}: showed up {member['showed_up']}/{member['window_days']} days{streak}")
    quiet = [f"@{m['handle']}" for m in status["members"] if m["showed_up"] == 0 and m["handle"]]
    if quiet:
        lines.append(f"{', '.join(quiet)} had a quieter week — a wave might help. \U0001f49b")
    return "\n".join(lines)


def _deliver_text(tenant, text: str) -> bool:
    """Deliver via the member's own channel (mirrors apps/core/services notify).
    A no-op when there's no linked surface. Patched in tests."""
    from apps.router.cron_delivery import resolve_user_channel

    user = getattr(tenant, "user", None)
    if user is None:
        return False
    channel = resolve_user_channel(user)
    if channel is None:
        return False
    if channel == "line":
        from apps.core.services import _send_line_text

        return _send_line_text(tenant, getattr(user, "line_user_id", "") or "", text)
    if channel == "app":
        # App-preferred user (iOS device registered): there's no Telegram/LINE
        # chat to push to, so recording a ProactiveOutbound row IS the delivery —
        # it fires the APNs wake-push and writes the ?since= feed row the app
        # drains. The old ``return True`` counted the weekly claim as delivered
        # while writing nothing, which silently dropped the digest for
        # token-holders once outbound routing became app-first. The digest text
        # is platform-authored (handles only, no PII entities), so it stores as-is.
        from apps.router.proactive_context import record_proactive_outbound

        row = record_proactive_outbound(
            tenant=tenant,
            channel="app",
            channel_user_id=str(getattr(user, "id", "") or ""),
            message_text=text,
            job_name="_mission:digest",
        )
        return row is not None
    from apps.router.services import send_telegram_message

    chat_id = getattr(user, "telegram_chat_id", None)
    return bool(chat_id) and send_telegram_message(chat_id, text)
