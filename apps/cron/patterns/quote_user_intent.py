"""`quote_user_intent` pattern: quote the user's stored text + warmth wrapper.

Distinct from ``pure_reminder`` in two ways:
  - The agent may add a brief warm wrapper around the quoted intent
    (e.g. "Just a heads up — you wanted to remember: <user_text>").
    The verbatim user_text must still appear in the outbound message.
  - Optionally allows a single fact-refresh call before composing
    (e.g. ``nbhd_calendar_list_events`` to pull the current calendar
    state so a stored "your appointment is at 3pm" reminder can be
    cross-checked against today's calendar).

Used when the user's stored intent contains facts that may benefit from
a current-state refresh at fire time — e.g. "every Friday remind me of
my next dentist appointment", where the text refers to ongoing context.

The refresh tool is constrained to a small allow-list (read-only, no
mutations) so a refresh can't cascade into autonomous task creation —
this is the same guardrail that prevents the 22:07 duplicate-task bug
documented in CONTINUITY_cron-typed-patterns.md.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from . import register_handler
from .base import PatternHandler, PatternPayload

# Tools allowed as ``refresh_facts_via`` — read-only queries only. Adding
# a tool here is a deliberate policy decision; the agent cannot supply a
# tool not in this set.
_REFRESH_TOOL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "nbhd_calendar_list_events",
        "nbhd_calendar_get_freebusy",
        "nbhd_gmail_list_messages",
        "nbhd_task_list",
        "nbhd_goal_list",
        "nbhd_daily_note_get",
    }
)

_DEFAULT_MODEL = "haiku-4.5"
_TURN_TIMEOUT_SECONDS = 45


class QuoteUserIntentPayload(PatternPayload):
    """Schema for a quote-user-intent cron's typed_payload."""

    text: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="The user's stored intent text. Must appear verbatim in the outbound message.",
    )
    refresh_facts_via: str | None = Field(
        None,
        description=(
            "Optional read-only tool to call before composing. Must be in "
            "the allowed set (calendar / gmail / task_list / goal_list / "
            "daily_note_get). Tool result is given to the agent as context "
            "for the warmth wrapper."
        ),
    )

    @field_validator("refresh_facts_via")
    @classmethod
    def _validate_refresh_tool(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if value not in _REFRESH_TOOL_ALLOWLIST:
            raise ValueError(f"refresh_facts_via must be one of {sorted(_REFRESH_TOOL_ALLOWLIST)}; got {value!r}")
        return value


class QuoteUserIntentHandler(PatternHandler):
    pattern = "quote_user_intent"
    payload_schema = QuoteUserIntentPayload

    def build_oc_data(
        self,
        payload: QuoteUserIntentPayload,
        *,
        tenant: Any,
        name: str,
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        text = payload.text.strip()

        if payload.refresh_facts_via:
            message = (
                "You are firing a scheduled reminder that the user "
                f"asked you to keep. First call `{payload.refresh_facts_via}` "
                "to pull current context. Then call `nbhd_send_to_user` "
                "exactly once with a short warm message that QUOTES the "
                "verbatim user intent below — the quoted text must appear "
                "in your message exactly as written. After sending, stop. "
                "Do not call any other tool. Do not create tasks, goals, "
                "or follow-up crons.\n\n"
                f"VERBATIM USER INTENT:\n{text}"
            )
        else:
            message = (
                "You are firing a scheduled reminder that the user "
                "asked you to keep. Call `nbhd_send_to_user` exactly once "
                "with a short warm message that QUOTES the verbatim user "
                "intent below — the quoted text must appear in your "
                "message exactly as written. After sending, stop. Do not "
                "call any other tool. Do not create tasks, goals, or "
                "follow-up crons.\n\n"
                f"VERBATIM USER INTENT:\n{text}"
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
                "lightContext": True,
                "toolsAllow": self.get_tools_allow(payload),
                "timeoutSeconds": _TURN_TIMEOUT_SECONDS,
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        }

    def get_tools_allow(self, payload: QuoteUserIntentPayload) -> list[str]:
        tools = ["nbhd_send_to_user"]
        if payload.refresh_facts_via:
            tools.append(payload.refresh_facts_via)
        return tools

    def get_prompt_injection(
        self,
        payload: QuoteUserIntentPayload,
        *,
        tenant: Any,
        name: str,
    ) -> str:
        return (
            "## Cron pattern: quote_user_intent\n"
            "This turn fires a scheduled reminder. Quote the user's stored "
            "intent verbatim in the outbound message. You may add a brief "
            "warm wrapper, but the verbatim text must appear unchanged. "
            "Do not create tasks, goals, or new crons during this turn."
        )

    def validate_outbound_message(
        self,
        content: str,
        payload: QuoteUserIntentPayload,
    ) -> tuple[bool, str | None]:
        expected = payload.text.strip()
        actual = (content or "").strip()
        if not expected:
            return True, None
        if expected in actual:
            return True, None
        return False, (
            f"Outbound message must contain the verbatim user intent. "
            f"Expected substring: {expected!r}. Got: {actual[:200]!r}"
        )


register_handler(QuoteUserIntentHandler())
