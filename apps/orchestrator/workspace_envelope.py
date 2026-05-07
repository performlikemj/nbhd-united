"""Tenant-state envelope rendered into ``workspace/USER.md``.

OpenClaw injects a fixed set of bootstrap files (AGENTS.md, USER.md, SOUL.md, ...)
into the system prompt on every agent turn. By writing the envelope into USER.md
on signal-driven refresh, every cron run and every chat reply gets up-to-date
goals/tasks/lessons context without baking it into individual cron messages.

Phase 2.5 of the proactive-coherence work — supersedes the per-cron envelope
that lived in ``config_generator._build_context_envelope``.

USER.md may already contain agent-written content (relationship observations,
personality notes). To preserve that, the platform-managed block is wrapped in
HTML-comment sentinels and merged with a three-case algorithm:

1. Empty / OpenClaw default boilerplate → write managed block alone.
2. Has BEGIN/END markers → replace only the managed region.
3. Has agent-written content but no markers → prepend managed region, keep
   the agent's content verbatim below.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from django.conf import settings
from django.core.cache import cache

from apps.orchestrator.azure_client import download_workspace_file, upload_workspace_file
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


# ─── Starter-content detection (preserved from config_generator's envelope) ──


_STARTER_MARKDOWN_CACHE: dict[str, str] = {}


def _starter_markdown(slug: str) -> str:
    """Return the unmodified seed markdown for a given starter doc slug.

    Cached to avoid repeated imports. Returns empty string if the slug isn't
    a known starter template.
    """
    global _STARTER_MARKDOWN_CACHE
    if not _STARTER_MARKDOWN_CACHE:
        from apps.journal.services import STARTER_DOCUMENT_TEMPLATES

        _STARTER_MARKDOWN_CACHE = {t["slug"]: t["markdown"] for t in STARTER_DOCUMENT_TEMPLATES}
    return _STARTER_MARKDOWN_CACHE.get(slug, "")


_STARTER_TASK_LINES: frozenset[str] = frozenset()


def _starter_task_lines() -> frozenset[str]:
    """Stripped open-task lines from the starter tasks template."""
    global _STARTER_TASK_LINES
    if not _STARTER_TASK_LINES:
        seed_md = _starter_markdown("tasks")
        _STARTER_TASK_LINES = frozenset(
            line.strip() for line in seed_md.splitlines() if line.lstrip().startswith("- [ ]")
        )
    return _STARTER_TASK_LINES


# ─── State fetchers ────────────────────────────────────────────────────────


def envelope_goals(tenant: Tenant, *, max_chars: int = 1500) -> str:
    """Active goals from ``Document(kind=goal, slug=goals)``, char-capped.

    Returns empty when the doc is missing, blank, or still contains the
    unmodified starter seed (the agent and tenant haven't curated real goals
    yet — injecting placeholder text would mislead downstream cron logic).
    """
    from apps.journal.models import Document

    doc = Document.objects.filter(tenant=tenant, kind=Document.Kind.GOAL, slug="goals").first()
    if not doc:
        return ""
    md = (doc.markdown or "").strip()
    if not md:
        return ""
    starter = _starter_markdown("goals").strip()
    if starter and md == starter:
        return ""
    if len(md) > max_chars:
        return md[:max_chars].rstrip() + "\n_(truncated — see goals doc for full text)_"
    return md


def envelope_open_tasks(tenant: Tenant, *, max_items: int = 25) -> str:
    """Open tasks (`- [ ]` items) from ``Document(kind=tasks, slug=tasks)``.

    Skips the placeholder bullets seeded by ``seed_default_documents_for_tenant``
    — those are tutorial prompts, not real tasks. A tenant who has only the
    starter tasks contributes no open tasks to the envelope.
    """
    from apps.journal.models import Document

    doc = Document.objects.filter(tenant=tenant, kind=Document.Kind.TASKS, slug="tasks").first()
    if not doc:
        return ""
    starter_lines = _starter_task_lines()
    open_items = [
        line
        for line in (doc.markdown or "").splitlines()
        if line.lstrip().startswith("- [ ]") and line.strip() not in starter_lines
    ]
    if not open_items:
        return ""
    if len(open_items) > max_items:
        kept = open_items[:max_items]
        return "\n".join(kept) + f"\n_(+{len(open_items) - max_items} more open tasks in tasks doc)_"
    return "\n".join(open_items)


def envelope_recent_lessons(tenant: Tenant, *, limit: int = 3) -> str:
    """Most recent approved lessons as one-line summaries."""
    from apps.lessons.models import Lesson

    lessons = list(Lesson.objects.filter(tenant=tenant, status="approved").order_by("-created_at")[:limit])
    if not lessons:
        return ""
    out: list[str] = []
    for lesson in lessons:
        text = (lesson.text or "").strip()
        if not text:
            continue
        first_line = text.splitlines()[0]
        if len(first_line) > 140:
            first_line = first_line[:137].rstrip() + "..."
        out.append(f"- {first_line}")
    return "\n".join(out)


# ─── Pillar fetchers (Phase 2.6) ───────────────────────────────────────────
#
# Each returns markdown for its section, or empty string when there's nothing
# meaningful to show. Gating by feature flag (``tenant.fuel_enabled`` /
# ``tenant.finance_enabled``) is handled in ``render_managed_region`` so the
# helpers themselves stay simple and unit-testable in isolation.


def envelope_fuel_state(tenant: Tenant, *, max_chars: int = 1000) -> str:
    """Today's planned workout, recent done workouts, body weight, sleep.

    Designed to fit two usage patterns:
    - Forward-planners: shows today's planned session prominently.
    - Retrospective loggers: shows recent done sessions so the agent can
      reason about cadence and recovery.
    """
    from datetime import date as _date
    from datetime import timedelta as _timedelta

    from apps.fuel.models import BodyWeightLog, SleepLog, Workout, WorkoutStatus

    today = _date.today()
    sections: list[str] = []

    planned_today = Workout.objects.filter(
        tenant=tenant,
        status=WorkoutStatus.PLANNED,
        date=today,
    ).first()
    if planned_today:
        bits = [f"- **Today**: {planned_today.activity} ({planned_today.category})"]
        if planned_today.scheduled_at:
            bits[0] += f" at {planned_today.scheduled_at.astimezone().strftime('%H:%M %Z')}"
        if planned_today.duration_minutes:
            bits[0] += f" — {planned_today.duration_minutes} min"
        sections.append("\n".join(bits))

    recent_done = list(
        Workout.objects.filter(
            tenant=tenant,
            status=WorkoutStatus.DONE,
            date__gte=today - _timedelta(days=14),
        ).order_by("-date")[:3]
    )
    if recent_done:
        lines = ["**Recent sessions** (last 14d):"]
        for w in recent_done:
            line = f"- {w.date.isoformat()} — {w.activity} ({w.category}"
            if w.rpe:
                line += f", RPE {w.rpe}"
            if w.duration_minutes:
                line += f", {w.duration_minutes}m"
            line += ")"
            lines.append(line)
        sections.append("\n".join(lines))

    last_weight = BodyWeightLog.objects.filter(tenant=tenant).order_by("-date").first()
    if last_weight:
        weight_line = f"- **Body weight**: {last_weight.weight_kg} kg ({last_weight.date.isoformat()})"
        prior = (
            BodyWeightLog.objects.filter(tenant=tenant, date__lte=last_weight.date - _timedelta(days=6))
            .order_by("-date")
            .first()
        )
        if prior and prior.weight_kg != last_weight.weight_kg:
            delta = float(last_weight.weight_kg) - float(prior.weight_kg)
            sign = "+" if delta > 0 else ""
            weight_line += f" — {sign}{delta:.1f} kg vs {prior.date.isoformat()}"
        sections.append(weight_line)

    last_sleep = SleepLog.objects.filter(tenant=tenant).order_by("-date").first()
    if last_sleep:
        sleep_line = f"- **Last sleep**: {last_sleep.duration_hours}h ({last_sleep.date.isoformat()})"
        if last_sleep.quality is not None:
            sleep_line += f", quality {last_sleep.quality}/5"
        sections.append(sleep_line)

    if not sections:
        return ""

    body = "\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n_(truncated — call nbhd_fuel_summary for full state)_"
    return body


def envelope_finance_state(tenant: Tenant, *, max_chars: int = 1000) -> str:
    """Total debt, active payoff plan, top-priority account, recent payment."""
    from datetime import date as _date
    from datetime import timedelta as _timedelta
    from decimal import Decimal

    from apps.finance.models import FinanceAccount, FinanceTransaction, PayoffPlan

    sections: list[str] = []

    active_accounts = list(FinanceAccount.objects.filter(tenant=tenant, is_active=True))
    if not active_accounts:
        return ""

    debts = [a for a in active_accounts if a.is_debt]
    total_debt = sum((a.current_balance for a in debts), Decimal("0"))

    summary_line = f"- **Active accounts**: {len(active_accounts)} ({len(debts)} debts)"
    if debts:
        summary_line += f", total debt **${total_debt:,.2f}**"
    sections.append(summary_line)

    plan = PayoffPlan.objects.filter(tenant=tenant, is_active=True).order_by("-created_at").first()
    if plan:
        plan_line = (
            f"- **Active plan**: {plan.strategy} — {plan.payoff_months} months, "
            f"payoff by {plan.payoff_date.isoformat()}, "
            f"${plan.monthly_budget:,.2f}/mo"
        )
        sections.append(plan_line)

    if debts:
        if plan and plan.strategy == PayoffPlan.Strategy.AVALANCHE:
            priority = max(debts, key=lambda a: a.interest_rate or Decimal("0"))
            priority_line = (
                f"- **Top-priority debt** (avalanche): {priority.nickname} — ${priority.current_balance:,.2f}"
            )
            if priority.interest_rate:
                priority_line += f" @ {priority.interest_rate}% APR"
        else:
            priority = min(debts, key=lambda a: a.current_balance)
            priority_line = (
                f"- **Top-priority debt** (snowball): {priority.nickname} — ${priority.current_balance:,.2f}"
            )
            if priority.minimum_payment:
                priority_line += f", min ${priority.minimum_payment:,.2f}/mo"
        sections.append(priority_line)

    today = _date.today()
    upcoming = [a for a in debts if a.due_day and 0 <= ((a.due_day - today.day) % 31) <= 7 and a.minimum_payment]
    if upcoming:
        due_lines = ["**Upcoming due dates** (next 7 days):"]
        for a in upcoming[:5]:
            due_lines.append(f"- {a.nickname} — ${a.minimum_payment:,.2f} on day {a.due_day}")
        sections.append("\n".join(due_lines))

    recent_tx = (
        FinanceTransaction.objects.filter(tenant=tenant, date__gte=today - _timedelta(days=14))
        .select_related("account")
        .order_by("-date", "-created_at")[:3]
    )
    recent_list = list(recent_tx)
    if recent_list:
        tx_lines = ["**Recent transactions**:"]
        for t in recent_list:
            tx_lines.append(f"- {t.date.isoformat()} — {t.transaction_type} ${t.amount:,.2f} → {t.account.nickname}")
        sections.append("\n".join(tx_lines))

    body = "\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n_(truncated — call nbhd_finance_summary for full state)_"
    return body


def envelope_recent_journal(tenant: Tenant, *, limit: int = 3, preview_chars: int = 250) -> str:
    """Last few daily-note documents, excluding today (still volatile).

    Today's daily note is the agent's working canvas during a turn — pre-baking
    a stale snapshot would mislead. The preamble's load instruction handles
    today's note via ``nbhd_daily_note_get``. This section gives the agent a
    glance of the last ~3 days for trend awareness.
    """
    from datetime import date as _date

    from apps.journal.models import Document

    today_iso = _date.today().isoformat()
    docs = list(
        Document.objects.filter(tenant=tenant, kind=Document.Kind.DAILY)
        .exclude(slug=today_iso)
        .order_by("-updated_at")[:limit]
    )
    if not docs:
        return ""

    lines: list[str] = []
    for doc in docs:
        title = (doc.title or doc.slug or "(untitled)").strip()
        body = (doc.markdown or "").strip()
        if not body:
            continue
        first = body.splitlines()[0].strip().lstrip("# ").strip()
        rest = " ".join(body.replace(first, "", 1).split())
        preview = (first + " — " + rest).strip(" —")
        if len(preview) > preview_chars:
            preview = preview[: preview_chars - 1].rstrip() + "…"
        lines.append(f"- **{title}**: {preview}")

    return "\n".join(lines)


def render_profile_section(tenant: Tenant) -> str:
    """Compact profile block — lines for fields the user has actually set.

    Pulls from ``tenant.user``: display_name, timezone, preferred_channel,
    locale (language), and city. Empty values are omitted so the block stays
    short.
    """
    user = getattr(tenant, "user", None)
    if user is None:
        return ""

    lines: list[str] = []
    display_name = (getattr(user, "display_name", "") or "").strip()
    if display_name and display_name != "Friend":
        lines.append(f"- Display name: {display_name}")

    user_tz = (getattr(user, "timezone", "") or "").strip()
    if user_tz and user_tz != "UTC":
        lines.append(f"- Timezone: {user_tz}")

    preferred_channel = (getattr(user, "preferred_channel", "") or "").strip()
    if preferred_channel:
        lines.append(f"- Preferred channel: {preferred_channel}")

    language = (getattr(user, "language", "") or "").strip()
    if language and language != "en":
        lines.append(f"- Language: {language}")

    city = (getattr(user, "location_city", "") or "").strip()
    if city:
        lines.append(f"- Location: {city}")

    if not lines:
        return ""
    return "## Profile\n" + "\n".join(lines) + "\n"


# ─── Managed-region rendering + merge ──────────────────────────────────────


def render_managed_region(tenant: Tenant) -> str:
    """The full managed block, sentinel markers included.

    Always present, even when the tenant has no envelope state — so the agent
    can rely on the markers existing in USER.md and so subsequent merges have
    something deterministic to replace.

    Pillar sections (Fuel, Finance) are gated by per-tenant feature flags so
    a tenant that hasn't enabled Gravity doesn't see a finance section taking
    up tokens for nothing.
    """
    profile = render_profile_section(tenant)
    goals = envelope_goals(tenant)
    tasks = envelope_open_tasks(tenant)
    lessons = envelope_recent_lessons(tenant)
    fuel = envelope_fuel_state(tenant) if getattr(tenant, "fuel_enabled", False) else ""
    finance = envelope_finance_state(tenant) if getattr(tenant, "finance_enabled", False) else ""
    journal = envelope_recent_journal(tenant)

    refreshed_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    parts: list[str] = [
        BEGIN_MARKER,
        "",
        "# Pre-loaded user state",
        "",
        f"_Last refreshed: {refreshed_at}_",
        "",
        "_Treat the sections below as a coherent snapshot. When responding, "
        "consider how Goals, Open tasks, Fuel, Finance, and recent Journal "
        "interact — don't reason about them as siloed lists._",
        "",
    ]

    if profile:
        parts.append(profile)

    has_state = any([goals, tasks, lessons, fuel, finance, journal])

    if has_state:
        if goals:
            parts.append("## Active goals")
            parts.append(goals)
            parts.append("")
        if tasks:
            parts.append("## Open tasks")
            parts.append(tasks)
            parts.append("")
        if fuel:
            parts.append("## Fuel — fitness state")
            parts.append(fuel)
            parts.append("")
        if finance:
            parts.append("## Gravity — finance state")
            parts.append(finance)
            parts.append("")
        if lessons:
            parts.append("## Recent lessons")
            parts.append(lessons)
            parts.append("")
        if journal:
            parts.append("## Recent journal")
            parts.append(journal)
            parts.append("")
    else:
        parts.append("_(No active goals, tasks, lessons, fuel, finance, or journal state yet.)_")
        parts.append("")

    parts.append(END_MARKER)
    parts.append("")  # trailing newline so concatenation is clean
    return "\n".join(parts)


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
