"""Living chat contract, deterministic policy/parsers, and redacted Jev service."""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import BoundedSemaphore, Lock
from time import monotonic
from types import SimpleNamespace
from typing import Literal
from uuid import UUID

from django.conf import settings
from django.utils import timezone
from pydantic import Field, field_validator, model_validator

from apps.common import jev
from apps.common.tenant_tz import tenant_tz, tenant_tz_name
from apps.router.chat_gates import chat_shape_enabled
from apps.router.panels import LOG_METRICS, PANEL_KINDS

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


PANELS = tuple(kind for kind in PANEL_KINDS if kind != "tasks")
FITNESS_SURFACES = frozenset({"training_week", "workout_detail", "body_weight"})
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
    "body_weight": "log_table",
    "journal": "journal_table",
}


class OpenPanel(jev.StrictModel):
    kind: Panel
    label: str = Field(max_length=4000)


class RecentTurn(jev.StrictModel):
    role: Literal["user", "assistant"]
    # Reject oversized raw fields; cutting here can turn a known PII value
    # into an unfamiliar fragment before the checked redactor sees it.
    text: str = Field(max_length=4000)


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
    metric: Literal[LOG_METRICS] | None = None
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
        if self.metric is not None and self.panel != "log_table":
            raise ValueError("Metric is only for log tables")
        if self.duration_seconds is not None and self.panel != "timer":
            raise ValueError("Duration is only for timers")
        if (self.reason == "ok") != (self.decision != "none"):
            raise ValueError("Reason must match decision")
        if (self.reason == "disabled") != (not self.enabled):
            raise ValueError("Enabled must match reason")
        return self


def chat_shape_panels() -> frozenset[str]:
    raw = str(getattr(settings, "CHAT_SHAPE_PANELS", "") or "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip()) & frozenset(PANELS)


def parse_date(text: str, tenant, *, now: datetime | None = None) -> tuple[TimeRange, date | None]:
    """Resolve explicit dates locally; unspecified means Jev may supply a range.

    This weekday is in the current Monday-based week; last is strictly past.
    Bare/on weekdays include today; 'next' is strictly in the future. Past N
    days selects the closest 1/7/30-day supported bucket (ties go shorter).
    The first explicit expression wins when a message names multiple periods.
    """
    today = (now or timezone.now()).astimezone(tenant_tz(tenant)).date()
    pattern = r"\b(today|yesterday|tomorrow|this\s+week|last\s+week|this\s+month|last\s+month|past\s+\d+\s+days?|(?:(?:next|on|last|this)\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return "unspecified", None
    token = " ".join(match[0].lower().split())
    if token in ("today", "yesterday", "tomorrow"):
        return token, today + timedelta(days={"today": 0, "yesterday": -1, "tomorrow": 1}[token])
    if token in {"this week", "last week", "this month", "last month"}:
        return token.replace(" ", "_"), None
    if token.startswith("past "):
        days = int(token.split()[1])
        return min(((1, "today"), (7, "this_week"), (30, "this_month")), key=lambda item: abs(item[0] - days))[1], None
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    weekday = weekdays.index(token.split()[-1])
    offset = (weekday - today.weekday()) % 7
    if token.startswith("this "):
        offset = weekday - today.weekday()
    elif token.startswith("last "):
        offset = -((today.weekday() - weekday) % 7 or 7)
    if token.startswith("next ") and offset == 0:
        offset = 7
    # Weekday has no range enum; day carries the date. Do not fall back to Jev.
    return "unspecified", today + timedelta(days=offset)


def parse_duration(text: str) -> int | None:
    """Sum adjacent components; never parse the suffix of a malformed number."""
    number = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?|an?|one"
    component = re.compile(
        rf"(?P<amount>{number})\s*(?P<unit>hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])",
        re.IGNORECASE,
    )
    initial = re.search(rf"(?<![\w.,+/-])(?:{component.pattern})", text, re.IGNORECASE)
    if initial is None or re.search(r"[0-9][\s,._/]*$|[+-]\s*$", text[: initial.start()]):
        return None
    match = initial
    total = Decimal(0)
    while match is not None:
        amount, unit = match["amount"].lower(), match["unit"].lower()
        value = Decimal(1) if amount in {"a", "an", "one"} else Decimal(amount.replace(",", ""))
        multiplier = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
        total += value * multiplier
        end = match.end()
        gap = re.match(r"\s*(?:and\s+)?", text[end:], re.IGNORECASE)
        next_start = end + gap.end()
        match = component.match(text, next_start)
        if match is None and unit == "h":
            # Compact hours/minutes notation: 1h30, optionally followed by 'm'.
            minutes = re.match(r"\s*([0-5]?[0-9])(?![\w]|[.,][0-9])", text[end:])
            if minutes:
                total += Decimal(minutes[1]) * 60
                end += minutes.end()
        if match is None:
            # A numeric continuation we cannot consume must not shorten a timer.
            if re.match(r"[\w]|[.,][0-9]|\s*(?:and\s+)?[0-9]", text[end:], re.IGNORECASE):
                return None
            break
    seconds = int(total)
    return seconds if seconds > 0 else None


def decide_panel(
    surface: jev.ChoiceAnswer,
    visual_helps: float,
    follow_up: float,
    resolved_range: TimeRange,
    open_panel: OpenPanel | None = None,
    enabled_panels: frozenset[str] = frozenset(PANELS),
) -> tuple[Decision, Panel | None, Reason]:
    """Resolve surfaces and fitness-family confidence; no I/O or hidden settings."""
    panel = SURFACE_PANELS.get(surface.choice)
    top_two = sorted(surface.probabilities, key=surface.probabilities.get, reverse=True)[:2]
    tie = (
        set(top_two) == {"training_week", "workout_detail"}
        and abs(surface.probabilities[top_two[0]] - surface.probabilities[top_two[1]]) <= 0.15 + 1e-9
        and resolved_range == "today"
    )
    family_confident = (
        surface.choice in FITNESS_SURFACES
        and sum(surface.probabilities.get(key, 0.0) for key in FITNESS_SURFACES) >= 0.6
    )
    if family_confident and surface.confidence < 0.5:
        panel = "log_table" if surface.probabilities.get("body_weight", 0.0) >= 0.6 else "training_week"
    if tie:
        panel = "workout"
    if panel is None or (visual_helps < 0.75 and follow_up < 0.5):
        return "none", None, "no_panel"
    if surface.confidence < 0.5 and not (tie or family_confident):
        return "none", None, "low_confidence"
    family = {"training_week", "workout"}
    same_family = open_panel is not None and (panel == open_panel.kind or {panel, open_panel.kind} <= family)
    decision = "update" if follow_up >= 0.5 and same_family else "open"
    if panel not in enabled_panels:
        return "none", None, "panel_disabled"
    return decision, panel, "ok"


logger = logging.getLogger(__name__)

SHAPE_BUDGET_SECONDS = 4.0
# Bounded admission: timed-out local inference cannot create unlimited workers
# or a queue of stale requests. A completed detector never starts late Jev work.
# Shared-socket operations have per-job timeouts, but Python cannot safely kill
# a thread stuck in native inference or HTTP/DNS. Such a job retains its slot;
# four permanent hangs disable shape until the application worker is restarted.
# Releasing/replacing occupied slots would allow unbounded abandoned work.
_SHAPE_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chat-shape")
_SHAPE_SLOTS = BoundedSemaphore(4)


class _ShapeTimings:
    """Snapshot elapsed stages even when a worker outlives the response."""

    def __init__(self):
        self._lock = Lock()
        self._elapsed = {"redact": 0.0, "jev": 0.0}
        self._active = None
        self.texts = 0

    @contextmanager
    def measure(self, stage):
        started = monotonic()
        with self._lock:
            self._active = (stage, started)
        try:
            yield
        finally:
            with self._lock:
                self._elapsed[stage] += monotonic() - started
                self._active = None

    def milliseconds(self):
        with self._lock:
            elapsed = self._elapsed.copy()
            if self._active is not None:
                stage, started = self._active
                elapsed[stage] += monotonic() - started
        return {stage: round(seconds * 1000, 3) for stage, seconds in elapsed.items()}


def shape_chat(payload: ChatShapeRequest, tenant, *, deadline: float | None = None) -> ChatShapeResponse:
    timings = _ShapeTimings()
    result = ChatShapeResponse(reason="unavailable")
    try:
        result = _run_shape_chat(payload, tenant, deadline=deadline, timings=timings)
    except Exception:
        # Never include payload, tenant data or exception details in telemetry.
        pass
    finally:
        elapsed = timings.milliseconds()
        logger.info(
            "chat_shape_timing redact_ms=%.3f jev_ms=%.3f reason=%s texts=%d",
            elapsed["redact"],
            elapsed["jev"],
            result.reason,
            timings.texts,
            extra={
                "redact_ms": elapsed["redact"],
                "jev_ms": elapsed["jev"],
                "reason": result.reason,
                "texts": timings.texts,
            },
        )
    return result


def _run_shape_chat(payload, tenant, *, deadline, timings):
    from apps.pii.ephemeral import warm_ephemeral_path

    if not chat_shape_enabled(tenant):
        return ChatShapeResponse(enabled=False, reason="disabled")
    deadline = deadline if deadline is not None else monotonic() + SHAPE_BUDGET_SECONDS
    if monotonic() >= deadline:
        return ChatShapeResponse(reason="unavailable")
    # Cold initialization may outlive several requests. It runs exactly once
    # outside the bounded pool, so expired callers cannot occupy all four slots
    # waiting for the same imports/model or enqueue more initialization jobs.
    with timings.measure("redact"):
        if not warm_ephemeral_path(deadline=deadline):
            return ChatShapeResponse(reason="unavailable")
    if monotonic() >= deadline or not _SHAPE_SLOTS.acquire(blocking=False):
        return ChatShapeResponse(reason="unavailable")
    # Snapshot on the request thread: background work has no ORM-capable tenant,
    # lazy relations, or mutable references into the caller's PII registry.
    try:
        snapshot = SimpleNamespace(
            id=tenant.id,
            model_tier=getattr(tenant, "model_tier", "starter"),
            pii_entity_map=deepcopy(getattr(tenant, "pii_entity_map", {}) or {}),
            pii_type_counters=deepcopy(getattr(tenant, "pii_type_counters", {}) or {}),
            pii_denylist=deepcopy(getattr(tenant, "pii_denylist", {}) or {}),
            user=SimpleNamespace(timezone=tenant_tz_name(tenant)),
        )
        panels = chat_shape_panels()
        future = _SHAPE_WORKERS.submit(_shape_chat, payload, snapshot, deadline, panels, timings)
    except Exception:
        _SHAPE_SLOTS.release()
        return ChatShapeResponse(reason="unavailable")
    future.add_done_callback(lambda _: _SHAPE_SLOTS.release())
    try:
        result = future.result(timeout=max(0, deadline - monotonic()))
        return result if monotonic() < deadline else ChatShapeResponse(reason="unavailable")
    except Exception:
        future.cancel()
        return ChatShapeResponse(reason="unavailable")


def truncate_redacted(text: str, limit: int = 300) -> str:
    """Shorten confirmed text, extending a cut to retain a whole placeholder.

    The limit is soft only for a placeholder intersecting it. This helper must
    never receive raw input; full-field checked redaction owns PII boundaries.
    """
    from apps.pii.redactor import _PLACEHOLDER_RE

    for match in _PLACEHOLDER_RE.finditer(text):
        if match.start() < limit < match.end():
            return text[: match.end()]
        if match.start() >= limit:
            break
    return text[:limit]


def _shape_chat(payload, tenant, deadline, panels, timings):
    from apps.pii.ephemeral import redact_texts_ephemeral_checked
    from apps.pii.redactor import as_confirmed

    # Fresh topics use only the latest message. Follow-ups confirm every full
    # history field and the panel label before selecting/shortening context.
    turns = payload.recent_turns if payload.open_panel is not None else []
    texts = [payload.text, *(turn.text for turn in turns)]
    if payload.open_panel is not None:
        texts.append(payload.open_panel.label)
    try:
        if monotonic() >= deadline:
            return ChatShapeResponse(reason="unavailable")
        timings.texts = len(texts)
        with timings.measure("redact"):
            outcomes = redact_texts_ephemeral_checked(texts, tenant, deadline=deadline)
        if monotonic() >= deadline:
            return ChatShapeResponse(reason="unavailable")
        confirmed = [as_confirmed(outcome) for outcome in outcomes]
        if len(confirmed) != len(texts) or any(item is None for item in confirmed):
            return ChatShapeResponse(reason="redaction_unconfirmed")
        redacted = [item.text for item in confirmed]
    except TimeoutError:
        return ChatShapeResponse(reason="unavailable")
    except Exception:
        return ChatShapeResponse(reason="redaction_unconfirmed")
    state = {
        "latest_message": redacted[0],
        "open_panel": (
            f"{payload.open_panel.kind}: {truncate_redacted(redacted[-1])}" if payload.open_panel else "none"
        ),
    }
    if payload.open_panel is not None:
        state["recent_turns"] = [
            f"{turn.role}: {truncate_redacted(text)}" for turn, text in list(zip(turns, redacted[1:]))[-2:]
        ]
    try:
        with timings.measure("jev"):
            answers = jev.decide(state, QUESTIONS, deadline=deadline).answers
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
            panels,
        )
        return ChatShapeResponse(
            decision=decision,
            panel=panel,
            metric="body_weight" if panel == "log_table" else None,
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
