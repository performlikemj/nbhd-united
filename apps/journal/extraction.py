"""End-of-day proactive extraction: goals, tasks, and lessons from daily notes.

Runs nightly via the 'Nightly Extraction' cron job. Reads today's daily note
(falling back to recent conversation pairs), calls a small LLM for structured
extraction, creates PendingExtraction records, and delivers Telegram inline
button prompts for user approval.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

import requests
from django.conf import settings
from django.utils import timezone

from apps.billing.services import record_usage
from apps.journal.models import DailyNote, Document, PendingExtraction
from apps.lessons.models import Lesson
from apps.lessons.services import generate_embedding
from apps.pii.redactor import rehydrate_text
from apps.router.extraction_callbacks import _approve_goal, _approve_lesson, _approve_task
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Sonnet 4.6 over gpt-4o-mini: reconciliation (matching journal evidence
# against an open-task list) is materially harder than emitting net-new
# items. One call per tenant per day; cost absorbed on the platform key.
EXTRACTION_MODEL = "anthropic/claude-sonnet-4-6"
MIN_NOTE_LENGTH = 100  # chars — below this we skip or fall back
DEDUP_SIMILARITY_THRESHOLD = 0.65  # cosine similarity for semantic dedup
TELEGRAM_API_BASE = "https://api.telegram.org/bot"
TELEGRAM_TIMEOUT = 10
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_TIMEOUT = 10


# ── LLM extraction ───────────────────────────────────────────────────────────

EXTRACTION_SYSTEM = """\
You are an assistant that extracts structured information from a user's daily notes or conversation log.

Return ONLY valid JSON matching this schema:
{
  "lessons": [{"text": "...", "context": "where/how this insight arose", "confidence": "high|medium", "tags": ["..."]}],
  "goals":   [{"text": "...", "confidence": "high|medium"}],
  "tasks":   [{"text": "...", "confidence": "high|medium"}]
}

Rules:
- Extract things the user stated or clearly implied through their writing.
- Lessons: personal insights, realizations, or things the user learned — what they now know or would do differently.
  Frame as advice to their future self — not what happened, but what to do differently.
  Include life lessons, professional insights, relationship realizations, health observations, and personal growth moments.
  Bad: "The PR photo was the wrong size (45x35cm instead of 40x30cm)"
  Good: "Always verify exact photo dimensions for government documents before proceeding — Japanese photo machines offer non-standard sizes"
- context: 1 sentence describing the situation that prompted this lesson.
- tags: 2-5 specific tags describing the SUBJECT DOMAIN of the lesson — the field or topic it belongs to (e.g. devops, python, cooking, finance, relationships, health, writing). Prefer concrete domain tags over behavioral ones. Avoid generic tags like habits, consistency, growth, mindset, or productivity unless the lesson is explicitly about personal development — a lesson about keeping env files in a stable location should be tagged devops and secrets-management, not habits.
- Goals: things the user wants to build, ship, or achieve (multi-day/week scope).
- Tasks: specific near-term action items with clear completion criteria.
- Ignore small talk, routine status updates, and things that are purely observational with no insight.
- Return empty arrays if nothing qualifies. Never force output.
- Keep each item concise (1-2 sentences max).
"""

# Reconciliation prompt: extends the base extraction with three additional
# arrays for state deltas against the tenant's existing typed Task/Goal
# rows. The user message carries the open items as JSON; the model
# matches journal evidence against ids and proposes per-item actions.
EXTRACTION_RECONCILE_SYSTEM = (
    EXTRACTION_SYSTEM.rstrip()
    + """

You will ALSO receive a JSON block titled "Open items" with the user's
current open tasks + active goals. Reconcile today's journal against
those items and return THREE additional arrays:

  "task_updates": [
    {"task_id": "<uuid from open_tasks>", "action": "complete|in_progress|skip|defer", "evidence": "<verbatim journal quote, <=140 chars>"}
  ],
  "subtasks_added": [
    {"parent_task_id": "<uuid from open_tasks>", "title": "<short>"}
  ],
  "goal_updates": [
    {"goal_id": "<uuid from active_goals>", "action": "achieve|abandon", "evidence": "<verbatim journal quote>"}
  ]

Reconciliation rules:
- task_id / goal_id / parent_task_id MUST be UUIDs from the provided "Open items" lists. Do not invent ids.
- "complete" requires explicit completion language ("did X", "finished Y", "X is done").
- "in_progress" requires evidence of partial progress without completion.
- "skip" requires explicit decision not to do it ("I'm not going to bother with X").
- "defer" requires intent to do it later ("doing X tomorrow", "pushing X to next week").
- "achieve" (goal): the user stated they reached the goal's outcome.
- "abandon" (goal): explicit decision to stop pursuing.
- Subtasks: only emit when the journal reveals work that is a sub-step of an open task.
- evidence MUST be a verbatim quote from the journal, <=140 chars.
- If you propose ANY task_update for an item, do NOT also list it under "tasks". Same for goals.
- If the journal mentions something that matches an existing open task by intent but the user did NOT change its state, omit it entirely — do not duplicate it as a new task.
- Be conservative. When in doubt, omit.
"""
)


def _call_extraction_llm(
    content: str,
    reconciliation_context: dict | None = None,
) -> tuple[dict, dict]:
    """Call LLM via OpenRouter and return (parsed extraction JSON, usage dict).

    When ``reconciliation_context`` is provided, the LLM also reconciles the
    journal against the supplied open items and returns task/goal deltas.
    """
    api_key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    if reconciliation_context is not None:
        system = EXTRACTION_RECONCILE_SYSTEM
        user_message = (
            f"Daily note:\n\n{content[:6000]}\n\nOpen items:\n\n{json.dumps(reconciliation_context, indent=2)[:4000]}"
        )
    else:
        system = EXTRACTION_SYSTEM
        user_message = f"Extract from this daily note:\n\n{content[:6000]}"

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": EXTRACTION_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = (data["choices"][0]["message"]["content"] or "").strip()
    usage = data.get("usage", {})
    # Strip markdown code fences if present (Claude via OpenRouter may wrap JSON)
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw), usage


# ── Source-of-truth resolution ───────────────────────────────────────────────


def _get_daily_note_content(tenant: Tenant, for_date: date) -> str | None:
    """Return today's daily note markdown if substantial enough."""
    # Try v2 Document first
    doc = Document.objects.filter(tenant=tenant, kind=Document.Kind.DAILY, slug=str(for_date)).first()
    if doc and len(doc.markdown) >= MIN_NOTE_LENGTH:
        return doc.markdown

    # Fall back to legacy DailyNote
    note = DailyNote.objects.filter(tenant=tenant, date=for_date).first()
    if note and len(note.markdown) >= MIN_NOTE_LENGTH:
        return note.markdown

    return None


def _get_fallback_content(tenant: Tenant) -> str | None:
    """Fall back to recent notes that haven't been extracted yet."""
    today = date.today()
    parts = []
    for delta in (0, 1):
        d = today - timedelta(days=delta)
        # Skip dates that already had successful extraction
        if (
            PendingExtraction.objects.filter(
                tenant=tenant,
                source_date=d,
            )
            .exclude(status="expired")
            .exists()
        ):
            continue
        content = _get_daily_note_content(tenant, d)
        if content:
            parts.append(f"## {d}\n{content}")
    combined = "\n\n".join(parts)
    return combined if len(combined) >= MIN_NOTE_LENGTH else None


# ── Deduplication ─────────────────────────────────────────────────────────────


def _is_duplicate(tenant: Tenant, kind: str, text: str) -> bool:
    """Return True if a very similar pending/approved item exists within 30 days."""
    cutoff = timezone.now() - timedelta(days=30)
    qs = PendingExtraction.objects.filter(
        tenant=tenant,
        kind=kind,
        created_at__gte=cutoff,
    ).exclude(status="expired")
    for existing in qs:
        # Simple substring dedup — good enough for now
        shorter, longer = sorted([text.lower(), existing.text.lower()], key=len)
        if shorter and shorter in longer:
            return True
    return False


def _existing_lesson_duplicate(tenant: Tenant, text: str) -> bool:
    """Return True if a similar lesson was approved/pending in the last 30 days."""
    cutoff = timezone.now() - timedelta(days=30)
    return Lesson.objects.filter(
        tenant=tenant,
        text__icontains=text[:50],
        created_at__gte=cutoff,
    ).exists()


def _embedding_duplicate(tenant: Tenant, text: str) -> bool:
    """Return True if a semantically similar approved lesson already exists."""
    from pgvector.django import CosineDistance

    try:
        embedding = generate_embedding(text)
    except Exception:
        logger.warning("embedding_duplicate: embedding generation failed, skipping semantic check")
        return False

    closest = (
        Lesson.objects.filter(tenant=tenant, status="approved", embedding__isnull=False)
        .annotate(distance=CosineDistance("embedding", embedding))
        .order_by("distance")
        .values_list("distance", flat=True)
        .first()
    )
    if closest is not None and (1.0 - float(closest)) >= DEDUP_SIMILARITY_THRESHOLD:
        return True
    return False


# ── Telegram delivery ─────────────────────────────────────────────────────────


def _send_telegram_with_buttons(
    bot_token: str,
    chat_id: int,
    text: str,
    buttons: list[list[dict]],
) -> int | None:
    """Send a Telegram message with inline keyboard. Returns message_id."""
    url = f"{TELEGRAM_API_BASE}{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": buttons},
    }
    try:
        resp = requests.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["result"]["message_id"]
    except Exception:
        logger.exception("Failed to send extraction Telegram message chat_id=%s", chat_id)
        return None


_TASK_ACTION_LABEL = {
    "task_complete": ("☑️", "marked done"),
    "task_progress": ("🟡", "marked in progress"),
    "task_skip": ("⏭️", "skipped"),
    "task_defer": ("📅", "deferred"),
    "subtask_create": ("➕", "added subtask"),
    "goal_achieve": ("🏁", "marked achieved"),
    "goal_abandon": ("🚫", "abandoned"),
}


def _format_task_action_line(action, entity_map: dict | None = None) -> str:
    """One-line summary for a reconciliation action, e.g. '☑️ Gym session — marked done'.

    ``entity_map`` (the tenant's ``pii_entity_map``) rehydrates any PII
    placeholder in the task/goal title before it is shown to the user —
    these titles are agent-authored and persist in placeholder space.
    """
    emoji, verb = _TASK_ACTION_LABEL.get(action.kind, ("•", action.kind))
    raw_title = action.task.title if action.task_id else action.goal.title if action.goal_id else "(unknown)"
    if entity_map:
        raw_title = rehydrate_text(raw_title, entity_map)
    title = raw_title[:60]
    return f"{emoji} {title} — {verb}"


def _deliver_summary_telegram(
    bot_token: str,
    chat_id: int,
    items: list[PendingExtraction],
    task_actions: list | None = None,
    entity_map: dict | None = None,
) -> None:
    """Send ONE summary message with per-item Remove buttons.

    When ``task_actions`` is non-empty (reconciliation deltas applied),
    they're rendered below the net-new extractions with their own
    ``task_action:undo:<id>`` callback prefix.
    """

    task_actions = task_actions or []
    kind_emoji = {"lesson": "💡", "goal": "🎯", "task": "✅"}
    lines = []
    buttons: list[list[dict]] = []

    if items:
        lines.append("From today's notes, I added:\n")
        for p in items:
            emoji = kind_emoji.get(p.kind, "•")
            ptext = rehydrate_text(p.text, entity_map) if entity_map else p.text
            lines.append(f"{emoji} {ptext}")
            undo_action = f"undo_{p.kind}"
            buttons.append(
                [
                    {
                        "text": f"Remove: {ptext[:30]}",
                        "callback_data": f"extract:{undo_action}:{p.id}",
                    }
                ]
            )

    if task_actions:
        if lines:
            lines.append("")
        lines.append("From today's journal, I also updated:\n")
        for a in task_actions:
            action_line = _format_task_action_line(a, entity_map)
            lines.append(action_line)
            buttons.append(
                [
                    {
                        "text": f"Undo: {action_line[:30]}",
                        "callback_data": f"task_action:undo:{a.id}",
                    }
                ]
            )

    if not (items or task_actions):
        return

    lines.append("\nTap Remove/Undo to revert any item.")
    text = "\n".join(lines)

    msg_id = _send_telegram_with_buttons(bot_token, chat_id, text, buttons)
    if msg_id:
        msg_id_str = str(msg_id)
        if items:
            for p in items:
                p.telegram_message_id = msg_id_str
            PendingExtraction.objects.bulk_update(items, ["telegram_message_id"])
        if task_actions:
            for a in task_actions:
                a.telegram_message_id = msg_id_str
            type(task_actions[0]).objects.bulk_update(task_actions, ["telegram_message_id"])


# ── LINE delivery ────────────────────────────────────────────────────────────


def _deliver_summary_line(
    channel_token: str,
    line_user_id: str,
    items: list[PendingExtraction],
    task_actions: list | None = None,
    entity_map: dict | None = None,
) -> bool:
    """Send a Flex Message carousel — one bubble per item with a Remove/Undo button.

    Reconciliation deltas (task_actions) render as additional bubbles in the
    same carousel with their own ``task_action:undo:<id>`` postback prefix.
    Returns True if delivery succeeded, False otherwise.
    """

    task_actions = task_actions or []
    kind_emoji = {"lesson": "💡", "goal": "🎯", "task": "✅"}
    bubbles = []

    # LINE carousel max is 10 bubbles total — prioritise extractions, fill
    # remainder with reconciliation actions
    remaining = 10
    items_to_send = items[:remaining]
    remaining -= len(items_to_send)
    actions_to_send = task_actions[:remaining]

    for p in items_to_send:
        emoji = kind_emoji.get(p.kind, "•")
        ptext = rehydrate_text(p.text, entity_map) if entity_map else p.text
        undo_action = f"undo_{p.kind}"
        label = re.sub(r"^[^\w]*", "", "Remove").strip()[:20]
        bubbles.append(
            {
                "type": "bubble",
                "size": "kilo",
                "styles": {
                    "body": {"backgroundColor": "#f6f4ee"},
                    "footer": {"backgroundColor": "#f6f4ee"},
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "16px",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"{emoji} {ptext[:120]}",
                            "wrap": True,
                            "size": "sm",
                            "color": "#12232c",
                        }
                    ],
                },
                "footer": {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "paddingAll": "12px",
                    "paddingTop": "0px",
                    "contents": [
                        {
                            "type": "button",
                            "style": "secondary",
                            "height": "sm",
                            "action": {
                                "type": "postback",
                                "label": label,
                                "data": f"extract:{undo_action}:{p.id}",
                                "displayText": f"Remove: {ptext[:30]}",
                            },
                        }
                    ],
                },
            }
        )

    for a in actions_to_send:
        line = _format_task_action_line(a, entity_map)
        bubbles.append(
            {
                "type": "bubble",
                "size": "kilo",
                "styles": {
                    "body": {"backgroundColor": "#f0eef9"},
                    "footer": {"backgroundColor": "#f0eef9"},
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "16px",
                    "contents": [
                        {
                            "type": "text",
                            "text": line[:120],
                            "wrap": True,
                            "size": "sm",
                            "color": "#12232c",
                        }
                    ],
                },
                "footer": {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "paddingAll": "12px",
                    "paddingTop": "0px",
                    "contents": [
                        {
                            "type": "button",
                            "style": "secondary",
                            "height": "sm",
                            "action": {
                                "type": "postback",
                                "label": "Undo",
                                "data": f"task_action:undo:{a.id}",
                                "displayText": f"Undo: {line[:30]}",
                            },
                        }
                    ],
                },
            }
        )

    if not bubbles:
        return True

    try:
        resp = requests.post(
            LINE_PUSH_URL,
            json={
                "to": line_user_id,
                "messages": [
                    {
                        "type": "flex",
                        "altText": "From today's notes, I added some items. Tap to undo.",
                        "contents": {"type": "carousel", "contents": bubbles},
                    }
                ],
            },
            headers={
                "Authorization": f"Bearer {channel_token}",
                "Content-Type": "application/json",
            },
            timeout=LINE_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(
                "LINE extraction summary push failed (%s): %s",
                resp.status_code,
                resp.text[:300],
            )
            return False
        return True
    except Exception:
        logger.exception("Failed to send extraction LINE summary user_id=%s", line_user_id)
        return False


# ── Channel resolution ───────────────────────────────────────────────────────


def _resolve_delivery_channel(tenant: Tenant) -> tuple[str, str | int | None, str | None]:
    """Determine delivery channel and credentials.

    Returns (channel, recipient_id, token) where channel is 'telegram' or 'line'.
    Returns ('none', None, None) if no channel is available.
    """
    preferred = getattr(tenant.user, "preferred_channel", "") or "telegram"
    chat_id = getattr(tenant.user, "telegram_chat_id", None)
    line_user_id = getattr(tenant.user, "line_user_id", None)

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    line_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "").strip()

    if preferred == "telegram" and chat_id and bot_token:
        return "telegram", chat_id, bot_token
    if preferred == "line" and line_user_id and line_token:
        return "line", line_user_id, line_token
    # Fallback to whichever is available
    if chat_id and bot_token:
        return "telegram", chat_id, bot_token
    if line_user_id and line_token:
        return "line", line_user_id, line_token
    return "none", None, None


# ── Core extraction runner ────────────────────────────────────────────────────


def run_extraction_for_tenant(tenant: Tenant) -> dict:
    """Run end-of-day extraction for a single tenant.

    For tenants with ``experimental_typed_journal_lifecycle`` on, the LLM
    is given today's journal *plus* the tenant's open Tasks + active
    Goals; its response carries both net-new extractions (lessons /
    goals / tasks) and reconciliation deltas
    (task_updates / subtasks_added / goal_updates) that get applied via
    typed-model mutations and recorded as ``PendingTaskAction`` rows.

    Returns: {"lessons": n, "goals": n, "tasks": n, "task_actions": n, "skipped": reason|None}
    """
    # Local imports — module-level imports of reconciliation symbols are
    # stripped by the lint-on-Edit hook when added in a separate patch
    # from their first usage. Keeping them local mirrors the existing
    # pattern for embed_daily_note / run_agenda_hint_pass below.
    from apps.journal.reconciliation import gather_reconciliation_context

    today = date.today()
    reconciling = bool(getattr(tenant, "experimental_typed_journal_lifecycle", False))

    # Resolve content
    content = _get_daily_note_content(tenant, today) or _get_fallback_content(tenant)
    if not content:
        logger.warning("extraction: no content for tenant %s, skipping", str(tenant.id)[:8])
        return {"lessons": 0, "goals": 0, "tasks": 0, "task_actions": 0, "skipped": "no_content"}

    logger.info(
        "extraction: tenant=%s content_length=%d reconciling=%s",
        str(tenant.id)[:8],
        len(content),
        reconciling,
    )

    # Resolve delivery channel (Telegram or LINE)
    channel, recipient_id, channel_token = _resolve_delivery_channel(tenant)
    logger.info(
        "extraction: tenant=%s channel=%s preferred=%s",
        str(tenant.id)[:8],
        channel,
        getattr(tenant.user, "preferred_channel", "unset"),
    )
    if channel == "none":
        logger.warning("extraction: no delivery channel for tenant %s, skipping", str(tenant.id)[:8])
        return {"lessons": 0, "goals": 0, "tasks": 0, "task_actions": 0, "skipped": "no_channel"}

    # Gather reconciliation context (typed-lifecycle tenants only)
    reconciliation_context = gather_reconciliation_context(tenant) if reconciling else None

    # Call LLM — one pass, returns both new items and (when reconciling) state deltas
    try:
        extracted, usage = _call_extraction_llm(content, reconciliation_context=reconciliation_context)
    except Exception:
        logger.exception("extraction: LLM call failed for tenant %s", str(tenant.id)[:8])
        return {"lessons": 0, "goals": 0, "tasks": 0, "task_actions": 0, "skipped": "llm_error"}

    # Attribute cost to tenant
    record_usage(
        tenant,
        event_type="extraction",
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        model_used=EXTRACTION_MODEL,
    )

    logger.info(
        "extraction: tenant=%s llm_result lessons=%d goals=%d tasks=%d",
        str(tenant.id)[:8],
        len(extracted.get("lessons", [])),
        len(extracted.get("goals", [])),
        len(extracted.get("tasks", [])),
    )

    expires_at = timezone.now() + timedelta(days=7)
    now = timezone.now()
    counts = {"lessons": 0, "goals": 0, "tasks": 0}
    added_items: list[PendingExtraction] = []

    # Process lessons — auto-add immediately
    for item in extracted.get("lessons", []):
        text = (item.get("text") or "").strip()
        if not text or len(text) < 20:
            continue
        if _existing_lesson_duplicate(tenant, text):
            continue
        if _is_duplicate(tenant, PendingExtraction.Kind.LESSON, text):
            continue
        if _embedding_duplicate(tenant, text):
            continue
        pending = PendingExtraction.objects.create(
            tenant=tenant,
            kind=PendingExtraction.Kind.LESSON,
            text=text,
            tags=item.get("tags", []),
            confidence=item.get("confidence", "medium"),
            expires_at=expires_at,
            status=PendingExtraction.Status.APPROVED,
            resolved_at=now,
            source_date=today,
        )
        _, lesson_id = _approve_lesson(pending)
        if lesson_id:
            pending.lesson_id = lesson_id
            pending.save(update_fields=["lesson_id"])
        added_items.append(pending)
        counts["lessons"] += 1

    # Process goals — auto-add immediately
    for item in extracted.get("goals", []):
        text = (item.get("text") or "").strip()
        if not text or len(text) < 20:
            continue
        if _is_duplicate(tenant, PendingExtraction.Kind.GOAL, text):
            continue
        pending = PendingExtraction.objects.create(
            tenant=tenant,
            kind=PendingExtraction.Kind.GOAL,
            text=text,
            confidence=item.get("confidence", "medium"),
            expires_at=expires_at,
            status=PendingExtraction.Status.APPROVED,
            resolved_at=now,
            source_date=today,
        )
        _approve_goal(pending)
        added_items.append(pending)
        counts["goals"] += 1

    # Process tasks — auto-add immediately
    for item in extracted.get("tasks", []):
        text = (item.get("text") or "").strip()
        if not text or len(text) < 10:
            continue
        if _is_duplicate(tenant, PendingExtraction.Kind.TASK, text):
            continue
        pending = PendingExtraction.objects.create(
            tenant=tenant,
            kind=PendingExtraction.Kind.TASK,
            text=text,
            confidence=item.get("confidence", "medium"),
            expires_at=expires_at,
            status=PendingExtraction.Status.APPROVED,
            resolved_at=now,
            source_date=today,
        )
        _approve_task(pending)
        added_items.append(pending)
        counts["tasks"] += 1

    # Reconciliation deltas — typed-lifecycle tenants only. Apply state
    # changes the LLM proposed against existing open Tasks / active Goals
    # and record each one as a PendingTaskAction for undo from the
    # morning summary.
    task_actions = []
    if reconciling:
        from apps.journal.reconciliation import apply_reconciliation_deltas

        task_actions = apply_reconciliation_deltas(tenant=tenant, deltas=extracted, source_date=today)
    counts["task_actions"] = len(task_actions)

    # Send ONE summary message — extractions + reconciliation actions in a single payload
    if added_items or task_actions:
        if channel == "telegram":
            _deliver_summary_telegram(
                channel_token, recipient_id, added_items, task_actions=task_actions, entity_map=tenant.pii_entity_map
            )
        elif channel == "line":
            ok = _deliver_summary_line(
                channel_token, recipient_id, added_items, task_actions=task_actions, entity_map=tenant.pii_entity_map
            )
            if not ok:
                # Fallback to Telegram if LINE delivery fails
                chat_id = getattr(tenant.user, "telegram_chat_id", None)
                bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
                if chat_id and bot_token:
                    logger.warning(
                        "extraction: LINE failed, falling back to Telegram for tenant %s", str(tenant.id)[:8]
                    )
                    _deliver_summary_telegram(
                        bot_token, chat_id, added_items, task_actions=task_actions, entity_map=tenant.pii_entity_map
                    )
    else:
        logger.warning(
            "extraction: tenant=%s zero items after dedup (raw: lessons=%d goals=%d tasks=%d, deltas=%d)",
            str(tenant.id)[:8],
            len(extracted.get("lessons", [])),
            len(extracted.get("goals", [])),
            len(extracted.get("tasks", [])),
            len(extracted.get("task_updates", []))
            + len(extracted.get("subtasks_added", []))
            + len(extracted.get("goal_updates", [])),
        )

    logger.info(
        "extraction: tenant=%s added lessons=%d goals=%d tasks=%d task_actions=%d channel=%s",
        str(tenant.id)[:8],
        counts["lessons"],
        counts["goals"],
        counts["tasks"],
        counts["task_actions"],
        channel,
    )

    # Embed today's daily note for contextual recall (best-effort)
    try:
        from apps.journal.embedding import embed_daily_note

        chunks_created = embed_daily_note(tenant, today)
        logger.info("extraction: embedded %d chunks for tenant %s", chunks_created, str(tenant.id)[:8])
    except Exception:
        logger.exception("extraction: embedding failed for tenant %s (non-fatal)", str(tenant.id)[:8])

    # Phase C: cross-domain agenda-hint pass — given today's journal +
    # the tenant's open agenda threads, classify which threads were
    # mentioned and how. Best-effort, fail-graceful — a hint-pass error
    # never affects the main extraction return.
    try:
        from apps.journal.agenda_hints import run_agenda_hint_pass

        hint_summary = run_agenda_hint_pass(tenant, content)
        if hint_summary.get("matches", 0):
            logger.info(
                "extraction: tenant=%s agenda hints %s",
                str(tenant.id)[:8],
                hint_summary,
            )
    except Exception:
        logger.exception("extraction: agenda-hint pass failed for tenant %s (non-fatal)", str(tenant.id)[:8])

    # Mark this tenant's nightly extraction as complete for their local day
    # so the hourly per-tz dispatcher (apps.orchestrator.tasks) doesn't fire
    # again within the same local 21-hour window. Records UTC now; the
    # dispatcher compares against tenant local-date.
    tenant.last_nightly_extraction_at = timezone.now()
    tenant.save(update_fields=["last_nightly_extraction_at"])

    return {**counts, "skipped": None}
