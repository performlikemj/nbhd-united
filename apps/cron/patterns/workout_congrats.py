"""`workout_congrats` pattern: agent-authored congratulations on a completed workout.

System-defined (not creatable by the agent's ``nbhd_cron_create_*`` tools). Fired
as a one-shot ``kind:"at"`` cron by ``apps/fuel/congrats.py`` a few seconds after a
user completes a workout via the JWT (console / iOS) path.

The message is ALWAYS agent-authored — never a hardcoded client string. We hand the
agent a minimal, PII-safe fact set (activity, category, duration, RPE, an optional
PR summary) and ask for ONE short, warm, personal congratulations referencing
something specific about the session. The anti-cascade guardrail is the same one the
other patterns use: ``toolsAllow=["nbhd_send_to_user"]`` so the turn cannot create
tasks / goals / crons, and ``delivery.mode:"none"`` so the send routes through Django
(``CronDeliveryView`` → LINE / iOS APNs push + ?since= feed), the one iOS-reachable
shape on this fleet.

Validation is deliberately lenient (non-empty, bounded) — unlike ``pure_reminder``
there is no verbatim string to echo; the whole point is a fresh, specific note. The
fallback is a canned, still-warm one-liner so a validation miss never sends nothing.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from apps.billing.constants import DEEPSEEK_FLASH_MODEL

from . import register_handler
from .base import PatternHandler, PatternPayload

# Typed-cron patterns fire a platform-initiated agent turn — pin the cheap
# NON-BYO worker model (DeepSeek V4 Flash), the same model the heartbeat and the
# TIER_TASK_DEFAULTS routine crons use. A congrats note needs no heavy reasoning,
# just warmth over a handful of facts. Flash is a member of every tier's
# agents.defaults.models allowlist (config_generator: HEARTBEAT_MODEL /
# TIER_MODEL_CONFIGS), so OpenClaw's cron preflight accepts it for starter,
# higher tiers, and BYO alike. Pin the full "openrouter/…" id (not a bare alias):
# it's the value the working platform crons already pin and the one that
# round-trips through preflight. The old hardcoded "haiku-4.5" sat in no non-BYO
# allowlist, so preflight rejected the fire-turn before nbhd_send_to_user — the
# congrats silently never fired for non-BYO fuel tenants.
_CRON_MODEL = DEEPSEEK_FLASH_MODEL
_TURN_TIMEOUT_SECONDS = 30

# Outbound bound — a congrats is 1-2 sentences; anything past this is drift.
_MAX_OUTBOUND_CHARS = 800


class WorkoutCongratsPayload(PatternPayload):
    """Minimal, PII-safe fact set for a completed-workout congratulation.

    Deliberately excludes free-text notes: cron prompts bypass inbound PII
    redaction, so only structured, low-sensitivity fields are embedded.
    """

    activity: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="The workout's activity name, e.g. 'Push — Chest & Shoulders'.",
    )
    category: str = Field(
        "",
        max_length=32,
        description="Workout category (strength/cardio/hiit/...). Empty if unknown.",
    )
    duration_minutes: int | None = Field(
        None,
        ge=0,
        le=100000,
        description="Session duration in minutes, if recorded.",
    )
    rpe: int | None = Field(
        None,
        ge=1,
        le=10,
        description="Rate of perceived exertion (1-10), if recorded.",
    )
    pr_summary: str = Field(
        "",
        max_length=200,
        description=(
            "Optional one-line summary of a personal record set in this session, "
            "e.g. 'New PR: Bench 100kg (prev 95kg)'. Empty when no PR."
        ),
    )


def _facts_line(payload: WorkoutCongratsPayload) -> str:
    """Render the embedded facts as a compact, human-readable clause."""
    bits: list[str] = [payload.activity.strip()]
    if payload.category:
        bits.append(f"({payload.category})")
    tail: list[str] = []
    if payload.duration_minutes is not None:
        tail.append(f"{payload.duration_minutes} min")
    if payload.rpe is not None:
        tail.append(f"RPE {payload.rpe}")
    head = " ".join(bits)
    if tail:
        head = f"{head} — {', '.join(tail)}"
    if payload.pr_summary.strip():
        head = f"{head}. {payload.pr_summary.strip()}"
    return head


class WorkoutCongratsHandler(PatternHandler):
    pattern = "workout_congrats"
    payload_schema = WorkoutCongratsPayload

    def build_oc_data(
        self,
        payload: WorkoutCongratsPayload,
        *,
        tenant: Any,
        name: str,
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        message = (
            "The user just completed this workout: "
            f"{_facts_line(payload)}.\n\n"
            "Send ONE short, warm, personal congratulations via "
            "`nbhd_send_to_user` — reference something specific about the "
            "workout, 1-2 sentences, no follow-up questions. Do not create "
            "tasks, goals, or crons. After the tool call completes, stop."
        )

        return {
            "name": name,
            "schedule": schedule,
            "sessionTarget": "isolated",
            "wakeMode": "next-heartbeat",
            "payload": {
                "kind": "agentTurn",
                "message": message,
                "model": _CRON_MODEL,
                "lightContext": True,
                "toolsAllow": self.get_tools_allow(payload),
                "timeoutSeconds": _TURN_TIMEOUT_SECONDS,
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        }

    def get_tools_allow(self, payload: WorkoutCongratsPayload) -> list[str]:
        return ["nbhd_send_to_user"]

    def get_outbound_contract(
        self,
        payload: WorkoutCongratsPayload,
        *,
        name: str,
    ) -> dict[str, Any]:
        return {
            "check": {"kind": "bounded", "max": _MAX_OUTBOUND_CHARS},
            "on_fail": {
                "action": "rewrite",
                "content": self.get_fallback_message(payload, name=name),
            },
        }

    def validate_outbound_message(
        self,
        content: str,
        payload: WorkoutCongratsPayload,
    ) -> tuple[bool, str | None]:
        actual = (content or "").strip()
        if not actual:
            return False, "Congrats message must be non-empty."
        if len(actual) > _MAX_OUTBOUND_CHARS:
            return False, (
                f"Congrats message is too long ({len(actual)} chars); keep it to "
                f"1-2 sentences (max {_MAX_OUTBOUND_CHARS})."
            )
        return True, None

    def get_fallback_message(self, payload: WorkoutCongratsPayload, *, name: str) -> str:
        return f"Nice work finishing {payload.activity.strip()}."


register_handler(WorkoutCongratsHandler())
