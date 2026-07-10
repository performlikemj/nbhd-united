"""Base classes for typed cron pattern handlers.

Each pattern (pure_reminder, quote_user_intent, domain_summary,
daily_briefing, workout_congrats) implements a subclass of
``PatternHandler`` and registers itself via ``register_handler()`` in
``apps/cron/patterns/__init__.py``.

Outbound enforcement is baked, not fetched. At save time,
``apps/cron/signals.py`` calls ``get_outbound_contract()`` and stamps the
result into ``CronJob.data["description"]`` as ``"nbhd.v1 " + json.dumps(...)``.
The ``nbhd-cron-enforcement`` OpenClaw plugin reads that contract off the
firing job (via the ``cron_changed`` hook) and evaluates every
``nbhd_send_to_user`` dispatch against it in-container, at
``before_tool_call`` — zero Django calls at fire time.

``validate_outbound_message()`` remains the canonical Python spec for the
same contract: it is no longer an RPC target (the old
``pattern_context`` / ``grounding`` / ``validate_outbound`` runtime
endpoints are gone — the plugin was their only caller), but it is the
Python side of the parity test that keeps the JS evaluator's
``contains`` / ``marker`` / ``bounded`` check kinds from drifting away
from what this file actually enforces.

The handler owns the full lifecycle for its pattern:

  build_oc_data()             — at create time, turn the typed payload
                                into an OC job dict (the ``data`` field
                                on CronJob).
  get_tools_allow()           — list of tools the agent turn is allowed
                                to call. This is the lever that prevents
                                cron-turn mutation cascades.
  get_outbound_contract()     — the declarative ``{check, on_fail}`` fire-time
                                contract, baked into ``data["description"]``
                                by the pre_save signal. ``None`` for patterns
                                with nothing to enforce (none currently — every
                                registered pattern defines one).
  validate_outbound_message() — the same contract, evaluated in Python.
                                Canonical spec for the parity test against
                                the plugin's JS evaluator; no longer called
                                at fire time.
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

    def get_outbound_contract(
        self,
        payload: PatternPayload,
        *,
        name: str,
    ) -> dict[str, Any] | None:
        """The declarative fire-time outbound contract for this pattern.

        Returns ``{"check": {...}, "on_fail": {...}}`` (see
        ``apps/cron/signals.py`` for the exact envelope it gets wrapped
        in — ``{"v": 1, "pattern": ..., **this}``). ``None`` means
        nothing is baked into ``data["description"]`` for this cron
        (today: never — every registered pattern defines a contract).

        ``check.kind`` is one of ``contains`` (``{text}``), ``marker``
        (``{marker}``), or ``bounded`` (``{max}``). ``on_fail.action`` is
        one of ``rewrite`` (``{content}``), ``revise_then_rewrite``
        (``{content, max_revisions}``), or ``revise_then_allow``
        (``{max_revisions}``). This shape MUST stay in lockstep with the
        ``nbhd-cron-enforcement`` plugin's ``evaluateCheck`` /
        ``decideGuardAction`` — see the parity test.

        Per-pattern matrix (the actual contracts each handler bakes):

          pure_reminder      — contains(text) / rewrite(text)
          quote_user_intent  — contains(text) / revise_then_rewrite(text, max_revisions=1)
          domain_summary     — marker([block: <render_block>]) / revise_then_allow(max_revisions=1)
          daily_briefing     — marker([block: daily_briefing]) / revise_then_allow(max_revisions=1)
          workout_congrats   — bounded(800) / rewrite(get_fallback_message())
        """
        return None

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
