"""Tenant-state envelope rendered into ``workspace/USER.md``.

OpenClaw injects a fixed set of bootstrap files (AGENTS.md, USER.md, SOUL.md, ...)
into the system prompt on every agent turn. By writing the envelope into USER.md
on signal-driven refresh, every cron run and every chat reply gets up-to-date
goals/tasks/lessons/fuel/finance/journal context without baking it into
individual cron messages.

USER.md may already contain agent-written content (relationship observations,
personality notes). To preserve that, the platform-managed block is wrapped in
HTML-comment sentinels and merged with a three-case algorithm:

1. Empty / OpenClaw default boilerplate → write managed block alone.
2. Has BEGIN/END markers → replace only the managed region.
3. Has agent-written content but no markers → prepend managed region, keep
   the agent's content verbatim below.

History:
- Phase 2.5 introduced this module — USER.md as the carrier, sentinel merge,
  goals/tasks/lessons/profile sections rendered inline.
- Phase 2.6 added Fuel / Finance / Recent journal sections (still inline).
- Phase 2.6.5 (this version) moves per-pillar rendering out to per-app
  ``envelope.py`` modules registered with :mod:`envelope_registry`. This
  module now owns only sentinel logic, the merge algorithm, and the
  push-to-file-share machinery; section content lives where the data does.
"""

from __future__ import annotations

import logging
import zoneinfo
from datetime import UTC, datetime

from django.conf import settings
from django.core.cache import cache

from apps.orchestrator.azure_client import download_workspace_file, upload_workspace_file
from apps.orchestrator.envelope_registry import all_sections
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


BEGIN_MARKER = (
    "<!-- BEGIN: NBHD-managed user state — do not edit between these markers; "
    "this region is rewritten by the platform on state changes. "
    "Write your own observations OUTSIDE these markers. -->"
)
END_MARKER = "<!-- END: NBHD-managed user state -->"


# OpenClaw seeds USER.md with this default when one isn't present. We treat it
# as "empty" so the first refresh replaces it cleanly rather than preserving
# the placeholder bullets as if they were agent-written content.
_OPENCLAW_DEFAULT_USER_MD = "# USER.md - User Profile\n\n- Name:\n- Preferred address:\n- Notes:\n"


_SYNTHESIS_HINT = (
    "_Treat the sections below as a coherent snapshot. When responding, "
    "consider how Goals, Open tasks, Fuel, Finance, and recent Journal "
    "interact — don't reason about them as siloed lists._"
)


def _render_current_time_line(tenant: Tenant) -> str:
    """Render the live ``Current local time`` line included on every USER.md push.

    Reads ``tenant.user.timezone`` — defaults to UTC on missing or
    unrecognised tz. Because USER.md is loaded by OpenClaw on every agent
    turn (chat and cron alike), this line is the authoritative live-time
    signal the assistant should consult, even when an upstream cron prompt
    embeds a stale snapshot. Bounded staleness comes from the periodic
    ``refresh_user_md_fleet`` QStash task plus signal-driven refreshes.
    """
    user_tz = str(getattr(getattr(tenant, "user", None), "timezone", "") or "UTC")
    try:
        tz = zoneinfo.ZoneInfo(user_tz)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")
        user_tz = "UTC"
    now = datetime.now(tz)
    return f"_Current local time: {now.strftime('%A, %B %d, %Y at %H:%M')} ({user_tz})_"


def render_managed_region(tenant: Tenant) -> str:
    """The full managed block, sentinel markers included.

    Walks the envelope registry in ``order`` ascending, calling each
    section's ``render`` when its ``enabled`` predicate is true and the
    body isn't empty. Markers are always present so subsequent merges
    have something deterministic to replace.

    Section content lives in per-pillar ``envelope.py`` modules — adding
    a new section is a one-file change there, not a render-loop edit.
    """
    refreshed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    current_time_line = _render_current_time_line(tenant)

    parts: list[str] = [
        BEGIN_MARKER,
        "",
        "# Pre-loaded user state",
        "",
        current_time_line,
        f"_Last refreshed: {refreshed_at}_",
        "",
        _SYNTHESIS_HINT,
        "",
    ]

    for section in all_sections():
        try:
            if not section.enabled(tenant):
                continue
            body = section.render(tenant)
        except Exception:
            # A misbehaving section shouldn't blow up the whole region.
            # Log + skip — agent still gets every other pillar's state.
            logger.exception(
                "Envelope section '%s' raised during render for tenant %s",
                section.key,
                str(tenant.id)[:8],
            )
            continue
        if not body:
            continue
        parts.append(section.heading)
        parts.append(body)
        parts.append("")

    parts.append(END_MARKER)
    parts.append("")  # trailing newline so concatenation is clean
    return "\n".join(parts)


# Context-digest size bounds (chars), owned here so the renderer's clamp and
# the API view's echoed ``max_chars`` can never drift apart. The default
# suits Apple's on-device foundation model (~4k-token window shared with
# tool schemas + transcript).
CONTEXT_DIGEST_DEFAULT_CHARS = 6000
CONTEXT_DIGEST_MIN_CHARS = 1000
CONTEXT_DIGEST_MAX_CHARS = 16000

# Sections that only make sense inside the tenant container's pipeline.
# ``privacy_placeholders`` instructs the model to emit ``[PERSON_N]``
# placeholders verbatim and promises a platform restoration layer — which
# exists on the relay path but NOT on a client device, where the digest is
# served rehydrated instead.
_CLIENT_DIGEST_SKIP_KEYS = frozenset({"privacy_placeholders"})


def render_context_digest(tenant: Tenant, *, max_chars: int = CONTEXT_DIGEST_DEFAULT_CHARS) -> str:
    """Compact plain-markdown snapshot of the tenant's state for clients that
    run their own model (the iOS private/on-device assistant).

    Same per-pillar sections as USER.md's managed region — goals, tasks,
    fuel, finance, recent journal, conversation digest — but rendered for a
    SMALL context window: no sentinel markers (nothing merges this back into
    a file), each section body is truncated, and the total is hard-capped.

    Budgeting: room for the conversation digest is reserved up front — it is
    the most load-bearing section for a client-side model (cross-channel
    conversational continuity) but renders late in display order, so without
    the reservation bulky early sections would starve it out. Other sections
    that don't fit are skipped, not a hard stop, so smaller later sections
    can still make the cut.
    """
    max_chars = max(CONTEXT_DIGEST_MIN_CHARS, min(int(max_chars), CONTEXT_DIGEST_MAX_CHARS))
    per_section = max(400, max_chars // 6)

    parts: list[str] = [_render_current_time_line(tenant), ""]
    total = sum(len(p) + 1 for p in parts)

    chunks: list[tuple[str, str]] = []
    for section in all_sections():
        if section.key in _CLIENT_DIGEST_SKIP_KEYS:
            continue
        try:
            if not section.enabled(tenant):
                continue
            body = section.render(tenant)
        except Exception:
            # Mirror render_managed_region: one broken section must not
            # blank the whole digest.
            logger.exception(
                "Context digest section '%s' raised for tenant %s",
                section.key,
                str(tenant.id)[:8],
            )
            continue
        if not body:
            continue
        if len(body) > per_section:
            body = body[: per_section - 1].rstrip() + "…"
        chunks.append((section.key, f"{section.heading}\n{body}\n"))

    reserved = 0
    for key, chunk in chunks:
        if key == "conversation_digest" and total + len(chunk) + 1 <= max_chars:
            reserved = len(chunk) + 1
            break

    for key, chunk in chunks:
        if key == "conversation_digest":
            if reserved:
                parts.append(chunk)
                total += len(chunk) + 1
                reserved = 0
            continue
        if total + len(chunk) + 1 + reserved > max_chars:
            continue
        parts.append(chunk)
        total += len(chunk) + 1

    return "\n".join(parts).strip() + "\n"


def _is_default_boilerplate(content: str) -> bool:
    """Detect OpenClaw's default seeded USER.md content.

    Treated as "empty" for merge purposes — replaced cleanly rather than
    preserved as if it were agent-written content.
    """
    return content.strip() == _OPENCLAW_DEFAULT_USER_MD.strip()


def merge_into_user_md(existing: str | None, managed: str) -> str:
    """Apply the three-case merge algorithm.

    1. ``existing`` is None / empty / OpenClaw boilerplate → managed alone.
    2. ``existing`` contains both markers → replace just the managed region.
    3. ``existing`` has content but no markers → prepend managed (with its
       markers), preserve everything else verbatim below.
    """
    if not existing or not existing.strip():
        return managed

    if _is_default_boilerplate(existing):
        return managed

    begin_idx = existing.find(BEGIN_MARKER)
    end_idx = existing.find(END_MARKER, begin_idx + len(BEGIN_MARKER)) if begin_idx >= 0 else -1

    if begin_idx >= 0 and end_idx > begin_idx:
        # Case 2: replace the managed region
        end_line_end = existing.find("\n", end_idx + len(END_MARKER))
        end_line_end = len(existing) if end_line_end < 0 else end_line_end + 1

        before = existing[:begin_idx]
        after = existing[end_line_end:]

        # Stitch: anything before the managed region (rare; usually empty),
        # then the freshly rendered managed block, then anything after.
        sep_before = "" if not before or before.endswith("\n") else "\n"
        sep_after = "\n" if after and not managed.endswith("\n") else ""
        return before + sep_before + managed + sep_after + after

    # Case 3: agent has written content but no markers exist yet — first
    # migration. Prepend managed block, preserve everything else.
    preserved = existing.lstrip("\n")
    return managed + "\n" + preserved


# ─── Push to file share, debounced ─────────────────────────────────────────


_DEFAULT_DEBOUNCE_SECONDS = 60

_DEBOUNCE_CACHE_PREFIX = "nbhd:user_md_pushed:"


def push_user_md(
    tenant: Tenant | str,
    *,
    debounce_seconds: int | None = None,
    force: bool = False,
) -> bool:
    """Render USER.md for the tenant, merge into existing file, write back.

    Returns True if the push happened, False if it was debounced.

    ``debounce_seconds`` (default 60) is a leading-edge debounce: the first
    call within a window writes; subsequent calls return False until the
    window expires. Pass ``force=True`` to bypass (used by post-deploy
    refresh sweeps and the ``update_system_cron_prompts`` integration).

    Read failures fall through to "no existing content" — the merge will
    write fresh managed content, which is safer than refusing to refresh.
    """
    if isinstance(tenant, Tenant):
        tenant_obj: Tenant | None = tenant
        tenant_id = str(tenant.id)
    else:
        tenant_obj = None
        tenant_id = str(tenant)

    window = _DEFAULT_DEBOUNCE_SECONDS if debounce_seconds is None else int(debounce_seconds)
    cache_key = f"{_DEBOUNCE_CACHE_PREFIX}{tenant_id}"

    if not force and window > 0 and cache.get(cache_key):
        logger.debug("USER.md push debounced for tenant %s (window=%ds)", tenant_id, window)
        return False

    # Set the debounce flag *before* doing work so concurrent callers see it.
    if window > 0:
        cache.set(cache_key, "1", timeout=window)

    try:
        if tenant_obj is None:
            tenant_obj = Tenant.objects.select_related("user").get(id=tenant_id)

        managed = render_managed_region(tenant_obj)

        try:
            existing = download_workspace_file(tenant_id, "workspace/USER.md")
        except Exception as exc:
            # Read failures shouldn't block writes — write fresh managed content.
            logger.warning(
                "Failed to read existing USER.md for tenant %s, writing fresh managed content: %s",
                tenant_id,
                exc,
            )
            existing = None

        merged = merge_into_user_md(existing, managed)
        upload_workspace_file(tenant_id, "workspace/USER.md", merged)
        logger.info("Pushed USER.md for tenant %s (%d chars)", tenant_id, len(merged))
        return True
    except Exception:
        # Clear the debounce flag on failure so the next call can retry.
        if window > 0:
            cache.delete(cache_key)
        raise


def push_user_md_in_background(tenant: Tenant | str) -> None:
    """Spawn a daemon thread to call ``push_user_md``.

    Mirrors the pattern in ``apps/journal/signals.py:queue_memory_sync_on_document_save``
    so post_save signals don't block the request thread on a file-share write.
    Failures are logged and swallowed; the next signal attempt will retry.
    """
    import threading

    tenant_id = str(tenant.id) if isinstance(tenant, Tenant) else str(tenant)

    def _run() -> None:
        try:
            push_user_md(tenant_id)
        except Exception:
            logger.warning(
                "Background USER.md push failed for tenant %s",
                tenant_id,
                exc_info=True,
            )

    if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
        # Synchronous fallback for tests and dev — same behavior, no thread.
        _run()
        return

    threading.Thread(target=_run, daemon=True).start()
