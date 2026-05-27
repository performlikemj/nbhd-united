"""Base classes for typed cron pattern handlers.

Each pattern (pure_reminder, quote_user_intent, domain_summary,
daily_briefing) implements a subclass of ``PatternHandler`` and
registers itself via ``register_handler()`` in ``apps/cron/patterns/__init__.py``.

The handler owns the full lifecycle for its pattern:

  build_oc_data()             — at create time, turn the typed payload
                                into an OC job dict (the ``data`` field
                                on CronJob).
  get_tools_allow()           — list of tools the agent turn is allowed
                                to call. This is the lever that prevents
                                cron-turn mutation cascades.
  get_prompt_injection()      — system-prompt addition shown at fire
                                time (injected via the enforcement
                                plugin's before_prompt_build hook,
                                appendSystemContext to avoid collision
                                with nbhd-routing-context which uses
                                prependSystemContext).
  validate_outbound_message() — invoked by the enforcement plugin's
                                message_sending hook to confirm the
                                outbound content satisfies the pattern's
                                contract (e.g., contains the verbatim
                                reminder text).
  get_fallback_message()      — what to send when validation fails
                                after the retry budget is exhausted.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel


class PatternPayload(BaseModel):
    """Marker base class for pattern payload schemas.

    Subclasses define the fields a pattern accepts. Validation is done
    at the entry points (runtime endpoints + signal pre-save guard) by
    constructing the subclass; Pydantic raises on bad input.
    """

    model_config = {
        "extra": "forbid",
        "str_strip_whitespace": True,
    }


class PatternHandler(ABC):
    """Abstract base for a typed cron pattern handler."""

    pattern: ClassVar[str]
    payload_schema: ClassVar[type[PatternPayload]]

    def validate_payload(self, raw_payload: dict[str, Any]) -> PatternPayload:
        """Parse + validate a raw payload dict into the typed schema."""
        return self.payload_schema(**raw_payload)

    @abstractmethod
    def build_oc_data(
        self,
        payload: PatternPayload,
        *,
        tenant: Any,
        name: str,
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        """Turn a validated payload into the OC job dict for CronJob.data.

        The returned dict is exactly what ``cron.add`` accepts as ``job``.
        Must include: name, schedule, sessionTarget, wakeMode, payload,
        delivery, enabled. Must NOT include id/createdAtMs/state.
        """

    @abstractmethod
    def get_tools_allow(self, payload: PatternPayload) -> list[str]:
        """Return the toolsAllow list for the agent turn at fire time.

        This is the structural constraint that prevents cron-turn
        cascades (e.g., morning briefing creating duplicate tasks).
        Default for most patterns: ``["nbhd_send_to_user"]`` only.
        """

    def get_prompt_injection(
        self,
        payload: PatternPayload,
        *,
        tenant: Any,
        name: str,
    ) -> str:
        """System-prompt addition shown at fire time.

        Returned as ``appendSystemContext`` from the enforcement plugin's
        ``before_prompt_build`` hook. Default: empty string (the message
        in the agent turn payload carries the instructions).
        """
        return ""

    @abstractmethod
    def validate_outbound_message(
        self,
        content: str,
        payload: PatternPayload,
    ) -> tuple[bool, str | None]:
        """Validate the outbound message content at message_sending time.

        Returns ``(True, None)`` on pass, ``(False, reason)`` on fail.
        Reason is fed back to the agent on a revise attempt and logged
        if the retry budget is exhausted.
        """

    def get_fallback_message(self, payload: PatternPayload, *, name: str) -> str:
        """Message to send when validation fails after the retry budget.

        Default: safe canned string that includes the cron's name so the
        user can find and edit / disable it.
        """
        return f"The scheduled message '{name}' couldn't be generated safely this time. It will retry on the next fire."
