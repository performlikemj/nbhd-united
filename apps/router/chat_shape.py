"""Living chat contract, deterministic policy/parsers, and redacted Jev service."""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from django.conf import settings
from django.utils import timezone
from pydantic import Field, field_validator, model_validator

from apps.common import jev
from apps.common.tenant_tz import tenant_tz

SURFACES = {
    "training_week": "Their workouts or training schedule across days: what sessions they did or have planned this week or month, streaks, missed sessions",
    "workout_detail": "One specific workout session: its exercises, sets, reps, run segments or how to do a movement",
    "nutrition": "What they ate or should eat: meals, calories, protein, macros, food log",
    "sleep": "How they slept: hours, bedtime, sleep quality or sleep trends",
    "body_weight": "Their body weight or body measurements over time",
    "spending": "Money they spent: transactions, spending by category, budgets, what they spent on something",
    "balances": "How much money they have: account balances, savings, net worth",
    "schedule": "Their calendar: events, appointments, what is on today, tomorrow or a given day, free time",
    "goals": "Long-term goals or life horizons and their progress",
    "journal": "Their journal or diary entries, mood over time, reflections they wrote",
    "place": "A specific place, venue, route or directions somewhere, or places nearby",
    "checklist": "A list of separate items or steps to tick off, such as a packing list, shopping list or to-do list being made now",
    "timer": "Starting a timer, focus session or countdown for a duration right now",
    "comparison": "Comparing two or more options side by side to choose between them",
    "none": "Casual talk, feelings, advice, open questions or anything where a plain text reply is best and no visual would help",
}

QUESTIONS = {
    "surface": {
        "type": "choice",
        "instructions": "The person is chatting with their personal assistant app. Which visual panel, if any, would best show them what they are asking about in `latest_message`? Use `recent_turns` and `open_panel` only to understand follow-ups.",
        "criteria": SURFACES,
    },
    "visual_helps": {
        "type": "score",
        "instructions": "How much would showing a visual panel of their own data or a tool help, compared with a plain text reply to `latest_message`?",
        "criteria": [
            "Not at all: this is conversation, feelings or advice best answered in words",
            "Somewhat: a visual could add a little but words are enough",
            "A lot: they are asking to see, track, compare or use something that is clearer as a visual",
        ],
    },
    "follow_up_on_open_panel": {
        "type": "noul",
        "instructions": "Is `latest_message` asking to change, filter, drill into or keep talking about the panel described in `open_panel`, rather than starting a new topic? If `open_panel` is none, answer no.",
    },
    "time_range": {
        "type": "choice",
        "instructions": "What time range does `latest_message` refer to, using `recent_turns` if it is a follow-up?",
        "criteria": {
            "today": "Today or right now",
            "tomorrow": "Tomorrow",
            "this_week": "This week or the last few days",
            "this_month": "This month or the last few weeks",
            "longer": "Several months, a year or all time",
            "unspecified": "No time range mentioned or implied",
        },
    },
    "wants_to_change": {
        "type": "noul",
        "instructions": "Does `latest_message` ask to add, edit, move, log or delete something, rather than only look at or ask about it?",
    },
}


PANELS = ("sleep", "schedule", "training_week", "workout", "timer")
RANGES = ("today", "yesterday", "tomorrow", "this_week", "last_week", "this_month", "last_month", "unspecified")
DECISIONS = ("open", "update", "none")
REASONS = ("ok", "disabled", "no_panel", "low_confidence", "redaction_unconfirmed", "unavailable", "panel_disabled")
Panel = Literal[PANELS]
TimeRange = Literal[RANGES]
Decision = Literal[DECISIONS]
Reason = Literal[REASONS]
SURFACE_PANELS = {
    "sleep": "sleep",
    "schedule": "schedule",
    "training_week": "training_week",
    "workout_detail": "workout",
    "timer": "timer",
}


class OpenPanel(jev.StrictModel):
    kind: Panel
    label: str


class RecentTurn(jev.StrictModel):
    role: Literal["user", "assistant"]
    text: str

    @field_validator("text")
    @classmethod
    def truncate_text(cls, value: str) -> str:
        return value[:500]


class ChatShapeRequest(jev.StrictModel):
    client_msg_id: str
    text: str = Field(max_length=2000)
    open_panel: OpenPanel | None = None
    recent_turns: list[RecentTurn] = Field(default_factory=list, max_length=4)

    @field_validator("client_msg_id")
    @classmethod
    def uuid_string(cls, value: str) -> str:
        UUID(value)
        return value


class ChatShapeResponse(jev.StrictModel):
    enabled: bool = True
    decision: Decision = "none"
    panel: Panel | None = None
    range: TimeRange = "unspecified"
    day: date | None = None
    duration_seconds: int | None = Field(default=None, gt=0)
    wants_change: jev.Probability = 0.0
    follow_up: jev.Probability = 0.0
    confidence: jev.Probability = 0.0
    reason: Reason = "no_panel"
    latency_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def consistent_decision(self):
        if (self.decision == "none") != (self.panel is None):
            raise ValueError("Panel must match decision")
        if self.duration_seconds is not None and self.panel != "timer":
            raise ValueError("Duration is only for timers")
        if (self.reason == "ok") != (self.decision != "none"):
            raise ValueError("Reason must match decision")
        if (self.reason == "disabled") != (not self.enabled):
            raise ValueError("Enabled must match reason")
        return self


def chat_shape_enabled(tenant) -> bool:
    raw = str(getattr(settings, "CHAT_SHAPE_TENANT_IDS", "") or "")
    allowed = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not allowed or tenant is None:
        return False
    return str(tenant.id).lower() in allowed


def chat_shape_panels() -> frozenset[str]:
    raw = str(getattr(settings, "CHAT_SHAPE_PANELS", "") or "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip()) & frozenset(PANELS)


def parse_date(text: str, tenant, *, now: datetime | None = None) -> tuple[TimeRange, date | None]:
    """Resolve explicit dates locally; unspecified means Jev may supply a range.

    Bare/on weekdays include today; 'next' is strictly in the future. Past N
    days selects the closest 1/7/30-day supported bucket (ties go shorter).
    The first explicit expression wins when a message names multiple periods.
    """
    today = (now or timezone.now()).astimezone(tenant_tz(tenant)).date()
    pattern = r"\b(today|yesterday|tomorrow|this\s+week|last\s+week|this\s+month|last\s+month|past\s+\d+\s+days?|(?:(?:next|on)\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return "unspecified", None
    token = " ".join(match[0].lower().split())
    if token in ("today", "yesterday", "tomorrow"):
        return token, today + timedelta(days={"today": 0, "yesterday": -1, "tomorrow": 1}[token])
    if token.startswith(("this ", "last ")):
        return token.replace(" ", "_"), None
    if token.startswith("past "):
        days = int(token.split()[1])
        return min(((1, "today"), (7, "this_week"), (30, "this_month")), key=lambda item: abs(item[0] - days))[1], None
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    offset = (weekdays.index(token.split()[-1]) - today.weekday()) % 7
    if token.startswith("next ") and offset == 0:
        offset = 7
    # Weekday has no range enum; day carries the date. Do not fall back to Jev.
    return "unspecified", today + timedelta(days=offset)


def parse_duration(text: str) -> int | None:
    match = re.search(
        r"(?<![\w.+-])(\d+(?:\.\d+)?|an?|one)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    amount, unit = (part.lower() for part in match.groups())
    value = Decimal(1) if amount in ("a", "an", "one") else Decimal(amount)
    multiplier = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
    seconds = int(value * multiplier)
    return seconds if seconds > 0 else None


def decide_panel(
    surface: jev.ChoiceAnswer,
    visual_helps: float,
    follow_up: float,
    resolved_range: TimeRange,
    open_panel: OpenPanel | None = None,
    enabled_panels: frozenset[str] = frozenset(PANELS),
) -> tuple[Decision, Panel | None, Reason]:
    """The directive's six rules, in order; no I/O or hidden settings."""
    panel = SURFACE_PANELS.get(surface.choice)
    top_two = sorted(surface.probabilities, key=surface.probabilities.get, reverse=True)[:2]
    tie = (
        set(top_two) == {"training_week", "workout_detail"}
        and abs(surface.probabilities[top_two[0]] - surface.probabilities[top_two[1]]) <= 0.15 + 1e-9
        and resolved_range == "today"
    )
    if tie:
        panel = "workout"
    if panel is None or (visual_helps < 0.75 and follow_up < 0.5):
        return "none", None, "no_panel"
    if surface.confidence < 0.5 and not tie:
        return "none", None, "low_confidence"
    family = {"training_week", "workout"}
    same_family = open_panel is not None and (panel == open_panel.kind or {panel, open_panel.kind} <= family)
    decision = "update" if follow_up >= 0.5 and same_family else "open"
    if panel not in enabled_panels:
        return "none", None, "panel_disabled"
    return decision, panel, "ok"


def shape_chat(payload: ChatShapeRequest, tenant) -> ChatShapeResponse:
    if not chat_shape_enabled(tenant):
        return ChatShapeResponse(enabled=False, reason="disabled")
    from apps.pii.redactor import as_confirmed, redact_user_message_checked

    # Labels are client text too: never let an open-panel label bypass redaction.
    texts = [payload.text, *(turn.text for turn in payload.recent_turns)]
    if payload.open_panel is not None:
        texts.append(payload.open_panel.label)
    redacted = []
    try:
        for text in texts:
            confirmed = as_confirmed(redact_user_message_checked(text, tenant, allow_user_name=False))
            if confirmed is None:
                return ChatShapeResponse(reason="redaction_unconfirmed")
            redacted.append(confirmed.text)
    except Exception:
        return ChatShapeResponse(reason="redaction_unconfirmed")
    state = {
        "latest_message": redacted[0],
        "recent_turns": [f"{turn.role}: {text}" for turn, text in zip(payload.recent_turns, redacted[1:])],
        "open_panel": f"{payload.open_panel.kind}: {redacted[-1]}" if payload.open_panel else "none",
    }
    try:
        answers = jev.decide(state, QUESTIONS).answers
        resolved_range, day = parse_date(payload.text, tenant)
        if resolved_range == "unspecified" and day is None:
            candidate = answers["time_range"].choice
            resolved_range = candidate if candidate in RANGES else "unspecified"
        surface = answers["surface"]
        follow_up = answers["follow_up_on_open_panel"].noul
        decision, panel, reason = decide_panel(
            surface,
            answers["visual_helps"].score,
            follow_up,
            resolved_range,
            payload.open_panel,
            chat_shape_panels(),
        )
        return ChatShapeResponse(
            decision=decision,
            panel=panel,
            reason=reason,
            range=resolved_range,
            day=day,
            duration_seconds=parse_duration(payload.text) if panel == "timer" else None,
            wants_change=answers["wants_to_change"].noul,
            follow_up=follow_up,
            confidence=surface.confidence,
        )
    except Exception:
        return ChatShapeResponse(reason="unavailable")
