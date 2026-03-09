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

from apps.journal.models import DailyNote, Document, PendingExtraction
from apps.lessons.models import Lesson
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

EXTRACTION_MODEL = "openai/gpt-4o-mini"
MIN_NOTE_LENGTH = 100  # chars — below this we skip or fall back
TELEGRAM_API_BASE = "https://api.telegram.org/bot"
TELEGRAM_TIMEOUT = 10
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_TIMEOUT = 10


# ── LLM extraction ───────────────────────────────────────────────────────────

EXTRACTION_SYSTEM = """\
You are an assistant that extracts structured information from a user's daily notes or conversation log.

Return ONLY valid JSON matching this schema:
{
  "lessons": [{"text": "...", "confidence": "high|medium", "tags": ["..."]}],
  "goals":   [{"text": "...", "confidence": "high|medium"}],
  "tasks":   [{"text": "...", "confidence": "high|medium"}]
}

Rules:
- Only extract things EXPLICITLY stated, never inferred.
- Lessons: insights, decisions, surprises, things that changed the user's approach.
- Goals: things the user wants to build, ship, or achieve (multi-day/week scope).
- Tasks: specific near-term action items with clear completion criteria.
- Ignore small talk, status updates, routine questions.
- Only include high or medium confidence items — skip anything vague.
- Return empty arrays if nothing qualifies. Never force output.
- Keep each item concise (1-2 sentences max).
"""


def _call_extraction_llm(content: str) -> dict:
    """Call LLM via OpenRouter and return parsed extraction JSON."""
    api_key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": EXTRACTION_MODEL,
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM},
                {"role": "user", "content": f"Extract from this daily note:\n\n{content[:6000]}"},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    return json.loads(raw)


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
    """Fall back to last 2 days of notes concatenated, or return None."""
    today = date.today()
    parts = []
    for delta in (0, 1):
        d = today - timedelta(days=delta)
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


def _deliver_lesson(bot_token: str, chat_id: int, pending: PendingExtraction) -> None:
    text = (
        f"💡 *Something worth remembering:*\n\n"
        f'"{pending.text}"'
    )
    buttons = [[
        {"text": "✅ Add to constellation", "callback_data": f"extract:approve_lesson:{pending.id}"},
        {"text": "❌ Skip", "callback_data": f"extract:dismiss:{pending.id}"},
    ]]
    msg_id = _send_telegram_with_buttons(bot_token, chat_id, text, buttons)
    if msg_id:
        pending.telegram_message_id = str(msg_id)
        pending.save(update_fields=["telegram_message_id"])


def _deliver_goal(bot_token: str, chat_id: int, pending: PendingExtraction) -> None:
    text = (
        f"🎯 *Noticed a new goal:*\n\n"
        f'"{pending.text}"'
    )
    buttons = [[
        {"text": "✅ Add to goals", "callback_data": f"extract:approve_goal:{pending.id}"},
        {"text": "❌ Skip", "callback_data": f"extract:dismiss:{pending.id}"},
    ]]
    msg_id = _send_telegram_with_buttons(bot_token, chat_id, text, buttons)
    if msg_id:
        pending.telegram_message_id = str(msg_id)
        pending.save(update_fields=["telegram_message_id"])


def _deliver_task(bot_token: str, chat_id: int, pending: PendingExtraction) -> None:
    text = (
        f"✅ *Action item detected:*\n\n"
        f'"{pending.text}"'
    )
    buttons = [[
        {"text": "✅ Add to tasks", "callback_data": f"extract:approve_task:{pending.id}"},
        {"text": "❌ Skip", "callback_data": f"extract:dismiss:{pending.id}"},
    ]]
    msg_id = _send_telegram_with_buttons(bot_token, chat_id, text, buttons)
    if msg_id:
        pending.telegram_message_id = str(msg_id)
        pending.save(update_fields=["telegram_message_id"])


# ── LINE delivery ────────────────────────────────────────────────────────────

def _send_line_with_buttons(
    channel_token: str,
    line_user_id: str,
    text: str,
    buttons: list[dict],
) -> bool:
    """Send a LINE Flex Message with postback buttons. Returns True on success."""
    # Split header and quoted body for visual hierarchy
    parts = text.split("\n\n", 1)
    header_text = parts[0].strip()
    body_text = parts[1].strip() if len(parts) > 1 else ""

    body_contents: list[dict] = [
        {
            "type": "text",
            "text": header_text,
            "wrap": True,
            "size": "sm",
            "weight": "bold",
            "color": "#12232c",
        },
    ]
    if body_text:
        body_contents.append({
            "type": "text",
            "text": body_text,
            "wrap": True,
            "size": "sm",
            "color": "#3d4f58",
            "margin": "md",
        })

    # Build button labels: strip emoji prefix for LINE label (20 char limit)
    # but keep displayText with emoji for the chat bubble
    def _clean_label(raw: str) -> str:
        """Strip leading emoji for LINE's 20-char label limit."""
        cleaned = re.sub(r"^[^\w]*", "", raw).strip()
        return cleaned[:20] if cleaned else raw[:20]

    flex_content = {
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
            "contents": body_contents,
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
                    "style": "primary" if i == 0 else "secondary",
                    "height": "sm",
                    **({"color": "#0D9488"} if i == 0 else {}),
                    "action": {
                        "type": "postback",
                        "label": _clean_label(btn["text"]),
                        "data": btn["callback_data"],
                        "displayText": btn["text"],
                    },
                }
                for i, btn in enumerate(buttons)
            ],
        },
    }
    try:
        resp = requests.post(
            LINE_PUSH_URL,
            json={
                "to": line_user_id,
                "messages": [{
                    "type": "flex",
                    "altText": text[:40],
                    "contents": flex_content,
                }],
            },
            headers={
                "Authorization": f"Bearer {channel_token}",
                "Content-Type": "application/json",
            },
            timeout=LINE_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(
                "LINE extraction push failed (%s): %s",
                resp.status_code,
                resp.text[:300],
            )
        return resp.status_code == 200
    except Exception:
        logger.exception("Failed to send extraction LINE message user_id=%s", line_user_id)
        return False


def _deliver_lesson_line(channel_token: str, line_user_id: str, pending: PendingExtraction) -> None:
    _send_line_with_buttons(channel_token, line_user_id, f'Something worth remembering:\n\n"{pending.text}"', [
        {"text": "✅ Add to constellation", "callback_data": f"extract:approve_lesson:{pending.id}"},
        {"text": "❌ Skip", "callback_data": f"extract:dismiss:{pending.id}"},
    ])


def _deliver_goal_line(channel_token: str, line_user_id: str, pending: PendingExtraction) -> None:
    _send_line_with_buttons(channel_token, line_user_id, f'Noticed a new goal:\n\n"{pending.text}"', [
        {"text": "✅ Add to goals", "callback_data": f"extract:approve_goal:{pending.id}"},
        {"text": "❌ Skip", "callback_data": f"extract:dismiss:{pending.id}"},
    ])


def _deliver_task_line(channel_token: str, line_user_id: str, pending: PendingExtraction) -> None:
    _send_line_with_buttons(channel_token, line_user_id, f'Action item detected:\n\n"{pending.text}"', [
        {"text": "✅ Add to tasks", "callback_data": f"extract:approve_task:{pending.id}"},
        {"text": "❌ Skip", "callback_data": f"extract:dismiss:{pending.id}"},
    ])


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

    Returns a summary dict: {"lessons": n, "goals": n, "tasks": n, "skipped": reason|None}
    """
    today = date.today()

    # Resolve content
    content = _get_daily_note_content(tenant, today) or _get_fallback_content(tenant)
    if not content:
        logger.info("extraction: no content for tenant %s, skipping", str(tenant.id)[:8])
        return {"lessons": 0, "goals": 0, "tasks": 0, "skipped": "no_content"}

    logger.info("extraction: tenant=%s content_length=%d", str(tenant.id)[:8], len(content))

    # Resolve delivery channel (Telegram or LINE)
    channel, recipient_id, channel_token = _resolve_delivery_channel(tenant)
    if channel == "none":
        logger.info("extraction: no delivery channel for tenant %s, skipping", str(tenant.id)[:8])
        return {"lessons": 0, "goals": 0, "tasks": 0, "skipped": "no_channel"}

    # Call LLM
    try:
        extracted = _call_extraction_llm(content)
    except Exception:
        logger.exception("extraction: LLM call failed for tenant %s", str(tenant.id)[:8])
        return {"lessons": 0, "goals": 0, "tasks": 0, "skipped": "llm_error"}

    logger.info(
        "extraction: tenant=%s llm_result lessons=%d goals=%d tasks=%d",
        str(tenant.id)[:8],
        len(extracted.get("lessons", [])),
        len(extracted.get("goals", [])),
        len(extracted.get("tasks", [])),
    )

    expires_at = timezone.now() + timedelta(days=7)
    counts = {"lessons": 0, "goals": 0, "tasks": 0}

    # Process lessons
    for item in extracted.get("lessons", []):
        text = (item.get("text") or "").strip()
        if not text or len(text) < 20:
            continue
        if _existing_lesson_duplicate(tenant, text):
            continue
        if _is_duplicate(tenant, PendingExtraction.Kind.LESSON, text):
            continue
        pending = PendingExtraction.objects.create(
            tenant=tenant,
            kind=PendingExtraction.Kind.LESSON,
            text=text,
            tags=item.get("tags", []),
            confidence=item.get("confidence", "medium"),
            expires_at=expires_at,
        )
        if channel == "telegram":
            _deliver_lesson(channel_token, recipient_id, pending)
        else:
            _deliver_lesson_line(channel_token, recipient_id, pending)
        counts["lessons"] += 1

    # Process goals
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
        )
        if channel == "telegram":
            _deliver_goal(channel_token, recipient_id, pending)
        else:
            _deliver_goal_line(channel_token, recipient_id, pending)
        counts["goals"] += 1

    # Process tasks
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
        )
        if channel == "telegram":
            _deliver_task(channel_token, recipient_id, pending)
        else:
            _deliver_task_line(channel_token, recipient_id, pending)
        counts["tasks"] += 1

    logger.info(
        "extraction: tenant=%s lessons=%d goals=%d tasks=%d",
        str(tenant.id)[:8], counts["lessons"], counts["goals"], counts["tasks"],
    )

    # Embed today's daily note for contextual recall (best-effort)
    try:
        from apps.journal.embedding import embed_daily_note
        chunks_created = embed_daily_note(tenant, today)
        logger.info("extraction: embedded %d chunks for tenant %s", chunks_created, str(tenant.id)[:8])
    except Exception:
        logger.exception("extraction: embedding failed for tenant %s (non-fatal)", str(tenant.id)[:8])

    return {**counts, "skipped": None}
