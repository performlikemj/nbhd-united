"""`pure_reminder` pattern: send a fixed text at the scheduled time.

The simplest pattern. The agent's role at fire time is reduced to
"call ``nbhd_send_to_user`` with this exact text and stop." No state
claims, no fact lookups, no derivation. The verbatim-echo invariant is
enforced by ``validate_outbound_message`` and by the agent turn's
``toolsAllow`` being a single-tool allowlist.

There is no native "no-LLM cron" mode in OC 2026.5.7 (confirmed in dist
inspection). To minimise cost we use ``haiku-4.5`` + ``lightContext: true``
+ a tightly-bounded ``toolsAllow`` — that's the lowest-cost agent turn
the runtime supports.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from . import register_handler
from .base import PatternHandler, PatternPayload


class PureReminderPayload(PatternPayload):
    """Schema for a pure-reminder cron's typed_payload."""

    text: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="The exact reminder text to send to the user, verbatim.",
    )


_REMINDER_MODEL = "haiku-4.5"
_TURN_TIMEOUT_SECONDS = 30


class PureReminderHandler(PatternHandler):
    pattern = "pure_reminder"
    payload_schema = PureReminderPayload

    def build_oc_data(
        self,
        payload: PureReminderPayload,
        *,
        tenant: Any,
        name: str,
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        text = payload.text.strip()

        # ``message`` is what the agent reads as its prompt at fire time.
        # The verbatim instruction is constructive (do this) AND prohibitive
        # (don't do anything else). The toolsAllow + finalize hook make the
        # prohibition structural; the prose makes it obvious to the model.
        message = (
            "You are firing a scheduled reminder. Call `nbhd_send_to_user` "
            "exactly once with the verbatim text below — no additions, no "
            "paraphrasing, no narration, no follow-up message. After the "
            "tool call completes, stop.\n\n"
            f"VERBATIM TEXT TO SEND:\n{text}"
        )

        return {
            "name": name,
            "schedule": schedule,
            "sessionTarget": "isolated",
            "wakeMode": "next-heartbeat",
            "payload": {
                "kind": "agentTurn",
                "message": message,
                "model": _REMINDER_MODEL,
                "lightContext": True,
                "toolsAllow": self.get_tools_allow(payload),
                "timeoutSeconds": _TURN_TIMEOUT_SECONDS,
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        }

    def get_tools_allow(self, payload: PureReminderPayload) -> list[str]:
        return ["nbhd_send_to_user"]

    def get_prompt_injection(
        self,
        payload: PureReminderPayload,
        *,
        tenant: Any,
        name: str,
    ) -> str:
        # Appended to system prompt at fire time via the enforcement plugin.
        # Belt-and-braces reinforcement of the verbatim rule — the model
        # sometimes ignores user-message constraints but obeys system-prompt
        # constraints more reliably.
        return (
            "## Cron pattern: pure_reminder\n"
            "This turn fires a scheduled reminder. Your only valid action is "
            "to call `nbhd_send_to_user` once with the verbatim text from "
            "the user message. Do not call any other tool. Do not add prose."
        )

    def validate_outbound_message(
        self,
        content: str,
        payload: PureReminderPayload,
    ) -> tuple[bool, str | None]:
        expected = payload.text.strip()
        actual = (content or "").strip()
        if not expected:
            # Empty expected text — vacuously pass; payload validation would
            # have rejected this at construction time anyway.
            return True, None
        if expected == actual:
            return True, None
        # Allow the model a tiny degree of slack: the expected text appears
        # verbatim somewhere in the output (e.g. wrapped in quotes). This
        # tolerance is intentionally narrow — we accept exact-match-or-
        # substring, not paraphrase.
        if expected in actual:
            return True, None
        return False, (
            f"Outbound message must contain the verbatim reminder text. Expected: {expected!r}. Got: {actual[:200]!r}"
        )


register_handler(PureReminderHandler())
