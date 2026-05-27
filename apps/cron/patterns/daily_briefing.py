"""`daily_briefing` pattern: morning summary, system-defined.

This pattern is NOT exposed via the agent's ``nbhd_cron_create_*``
tools — it's a system pattern, registered automatically for a tenant
when their morning briefing is set up. The agent can never create one.

The cron-creation hardening that ships in this PR (this handler's
``toolsAllow`` containing only ``nbhd_send_to_user`` plus read-only
queries) is the structural fix for the 22:07-style cascade
documented in CONTINUITY_cron-typed-patterns.md: the briefing's agent
turn cannot call ``nbhd_task_create`` / ``nbhd_task_complete`` / any
other mutation, so it cannot autonomously duplicate or close items.

Backend-rendered fact injection (the second half of the original bug
fix — preventing the briefing from claiming overdue items that the DB
shows as done) lands in a follow-up PR. That work adds a
``/runtime/<tenant>/briefing/facts/`` endpoint queried by the
enforcement plugin's ``before_prompt_build`` hook, returning a fresh
structured snapshot the agent must use as its source of truth. The
hook surface for the injection (appendSystemContext) is wired in this
PR's enforcement plugin; the facts endpoint is the follow-up.

For v1 the cron's prompt instructs the agent to call ``nbhd_task_list``
itself for current state, with explicit guardrails against
fabricating items not in the typed result.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from . import register_handler
from .base import PatternHandler, PatternPayload

_DEFAULT_MODEL = "sonnet-4.6"  # briefings warrant a stronger model than reminders
_TURN_TIMEOUT_SECONDS = 90

# Read-only query tools the briefing may call. Mutations are absent
# from this list by design.
_BRIEFING_QUERY_TOOLS: tuple[str, ...] = (
    "nbhd_task_list",
    "nbhd_goal_list",
    "nbhd_lessons_pending",
    "nbhd_calendar_list_events",
    "nbhd_daily_note_get",
    "nbhd_sessions_pending",
)

_ALLOWED_SECTIONS: frozenset[str] = frozenset(
    {
        "overdue_tasks",
        "due_today",
        "pending_lessons",
        "calendar_today",
        "weight_checkin",
        "weather",
    }
)

_ALLOWED_WARMTH: frozenset[str] = frozenset({"formal", "warm", "playful"})


class DailyBriefingPayload(PatternPayload):
    """Schema for a daily-briefing cron's typed_payload.

    Carries the briefing's editorial parameters but no facts — facts
    come from fresh queries at fire time, not from the stored payload.
    """

    sections: list[str] = Field(
        default_factory=lambda: [
            "overdue_tasks",
            "due_today",
            "pending_lessons",
            "calendar_today",
        ],
        description="Ordered list of sections to include in the briefing.",
    )
    warmth_level: str = Field(
        default="warm",
        description="Editorial tone — formal / warm / playful.",
    )

    @field_validator("sections")
    @classmethod
    def _validate_sections(cls, value: list[str]) -> list[str]:
        bad = [s for s in value if s not in _ALLOWED_SECTIONS]
        if bad:
            raise ValueError(f"Unknown briefing section(s): {bad}. Allowed: {sorted(_ALLOWED_SECTIONS)}")
        return value

    @field_validator("warmth_level")
    @classmethod
    def _validate_warmth(cls, value: str) -> str:
        if value not in _ALLOWED_WARMTH:
            raise ValueError(f"warmth_level must be one of {sorted(_ALLOWED_WARMTH)}; got {value!r}")
        return value


class DailyBriefingHandler(PatternHandler):
    pattern = "daily_briefing"
    payload_schema = DailyBriefingPayload

    def build_oc_data(
        self,
        payload: DailyBriefingPayload,
        *,
        tenant: Any,
        name: str,
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        sections_block = "\n".join(f"  - {s}" for s in payload.sections)
        message = (
            "You are composing the user's morning briefing. Tone: "
            f"{payload.warmth_level}. Compose ONE concise message — mobile-"
            "readable, scannable, leading with the most time-sensitive item.\n"
            "\n"
            "Sections (render only those with content; omit empty sections):\n"
            f"{sections_block}\n"
            "\n"
            "STRICT FACT-SOURCING RULES (these prevent the 'briefing claims "
            "overdue while DB says done' bug):\n"
            "  - To list overdue tasks, you MUST call `nbhd_task_list` first "
            "and surface ONLY tasks the typed result shows as open and past "
            "due (server-local date). Do not invent items, do not surface "
            "items that came back as `done` or `skipped`.\n"
            "  - To count pending lessons, you MUST call `nbhd_lessons_pending` "
            "first and surface its count verbatim. Do not estimate.\n"
            "  - To mention today's calendar, you MUST call "
            "`nbhd_calendar_list_events` first and quote event titles/times "
            "as returned. Do not paraphrase times.\n"
            "  - Every factual claim in the briefing must trace to a tool "
            "result from this turn. Anything you can't ground via a tool "
            "call, omit.\n"
            "  - You may NOT create tasks, goals, finance records, or "
            "follow-up crons during this turn — only render the briefing.\n"
            "\n"
            "When the briefing is composed, call `nbhd_send_to_user` exactly "
            "once. The first line of the outbound message must include the "
            "literal marker `[block: daily_briefing]` so downstream tooling "
            "can identify the render type."
        )

        return {
            "name": name,
            "schedule": schedule,
            "sessionTarget": "isolated",
            "wakeMode": "next-heartbeat",
            "payload": {
                "kind": "agentTurn",
                "message": message,
                "model": _DEFAULT_MODEL,
                "lightContext": False,
                "toolsAllow": self.get_tools_allow(payload),
                "timeoutSeconds": _TURN_TIMEOUT_SECONDS,
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        }

    def get_tools_allow(self, payload: DailyBriefingPayload) -> list[str]:
        # nbhd_send_to_user + read-only queries. No mutations. The
        # explicit list is the structural guard: even if the prompt
        # somehow drifts to encourage mutation, the runtime can't
        # execute it because the tools aren't in the allowlist.
        return ["nbhd_send_to_user", *_BRIEFING_QUERY_TOOLS]

    def get_prompt_injection(
        self,
        payload: DailyBriefingPayload,
        *,
        tenant: Any,
        name: str,
    ) -> str:
        return (
            "## Cron pattern: daily_briefing\n"
            "This turn renders today's morning briefing. Every factual "
            "claim must come from a tool call made in this turn — do not "
            "fabricate counts, dates, or item titles. Do not mutate state."
        )

    def validate_outbound_message(
        self,
        content: str,
        payload: DailyBriefingPayload,
    ) -> tuple[bool, str | None]:
        marker = "[block: daily_briefing]"
        if marker in (content or ""):
            return True, None
        return False, (f"Briefing outbound must include the marker {marker!r}. Got: {(content or '')[:200]!r}")


register_handler(DailyBriefingHandler())
