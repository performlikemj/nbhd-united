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
shows as done) remains a follow-up. The ``nbhd-cron-enforcement`` plugin
no longer has a prompt-injection hook (it only enforces the OUTBOUND
message at ``before_tool_call`` now — see ``get_outbound_contract``
below); a future fact-grounding mechanism would need its own
pre-generation surface, not this plugin.

For v1 the cron's prompt instructs the agent to call ``nbhd_task_list``
itself for current state, with explicit guardrails against
fabricating items not in the typed result.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from apps.billing.constants import DEEPSEEK_FLASH_MODEL

from . import register_handler
from .base import (
    DATEBOOK_CALENDAR_READ_TOOL,
    PatternHandler,
    PatternPayload,
    calendar_read_tool_for_tenant,
)

# Like the other typed-cron patterns (see #1167 / pure_reminder), the briefing
# fires a platform-initiated agent turn, so its model MUST sit in the firing
# tenant's agents.defaults.models allowlist or OpenClaw's cron preflight rejects
# the turn ("payload.model rejected by agents.defaults.models allowlist") before
# nbhd_send_to_user runs — no briefing is delivered. DeepSeek V4 Flash is a
# member of every tier's allowlist (config_generator: HEARTBEAT_MODEL /
# TIER_MODEL_CONFIGS), so preflight accepts it for starter, higher tiers, and BYO
# alike. Pin the full "openrouter/…" id (the value the working platform crons
# already use), which round-trips through preflight.
#
# Deliberate tradeoff (MJ's decision, 2026-07-12): briefings ride this cheap,
# allowlist-safe model rather than a stronger off-allowlist model like the old
# hardcoded "sonnet-4.6" — that only worked for tenants with an active BYO
# Anthropic credential and was preflight-rejected for everyone else (same failure
# class #1167 fixed). Briefing quality on the cheaper model is accepted until a
# BYO-aware resolver can pick a stronger model where the allowlist permits it.
_CRON_MODEL = DEEPSEEK_FLASH_MODEL
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
        calendar_tool = calendar_read_tool_for_tenant(tenant)
        if calendar_tool == DATEBOOK_CALENDAR_READ_TOOL:
            calendar_instruction = "`nbhd_datebook_read` with `days_ahead=0, entity='events'` first"
        else:
            calendar_instruction = "`nbhd_calendar_list_events` first"
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
            f"{calendar_instruction} and quote event titles/times "
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
                "model": _CRON_MODEL,
                "lightContext": False,
                "toolsAllow": self.get_tools_allow(payload, tenant=tenant),
                "timeoutSeconds": _TURN_TIMEOUT_SECONDS,
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        }

    def get_tools_allow(
        self,
        payload: DailyBriefingPayload,
        *,
        tenant: Any = None,
    ) -> list[str]:
        # nbhd_send_to_user + read-only queries. No mutations. The
        # explicit list is the structural guard: even if the prompt
        # somehow drifts to encourage mutation, the runtime can't
        # execute it because the tools aren't in the allowlist.
        calendar_tool = calendar_read_tool_for_tenant(tenant)
        query_tools = [calendar_tool if tool == "nbhd_calendar_list_events" else tool for tool in _BRIEFING_QUERY_TOOLS]
        return ["nbhd_send_to_user", *query_tools]

    def get_outbound_contract(
        self,
        payload: DailyBriefingPayload,
        *,
        name: str,
    ) -> dict[str, Any]:
        return {
            "check": {"kind": "marker", "marker": "[block: daily_briefing]"},
            "on_fail": {"action": "revise_then_allow", "max_revisions": 1},
        }

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
