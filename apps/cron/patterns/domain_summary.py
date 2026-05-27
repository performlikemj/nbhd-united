"""`domain_summary` pattern: scheduled summary of a domain's current state.

Distinct from ``quote_user_intent``: the cron's purpose is to render the
current state of a specific domain (fuel, journal, tasks, goals, lessons)
at fire time. The agent must call the registered ``query_tool`` first,
then ``nbhd_send_to_user`` with a rendering of the result.

The query tool is restricted to a hard-coded whitelist
(``DOMAIN_SUMMARY_QUERY_TOOLS``) — adding a new entry is a deliberate
policy decision and ships in a PR, not a runtime config change. This
prevents the agent from referencing arbitrary tools (including mutation
tools) as a "query" source.

Render-side validation: each whitelisted query tool declares a
``render_block`` type. The agent's outbound message must mention that
block type (e.g. ``[block: fuel_summary]``) so downstream tooling can
identify which structured render was used. This is a v1 minimum; a
full block-protocol renderer would replace string-marker validation
with structured blocks at the message layer.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from . import register_handler
from .base import PatternHandler, PatternPayload

# Whitelist of allowed query tools. Each entry pairs a tool name with
# the render_block tag the agent's outbound message must include. Adding
# a tool here is a deliberate policy decision.
DOMAIN_SUMMARY_QUERY_TOOLS: dict[str, dict[str, str]] = {
    "nbhd_task_list": {
        "render_block": "task_summary",
        "description": "Open / done / skipped tasks (filterable by status, pillar, due range).",
    },
    "nbhd_goal_list": {
        "render_block": "goal_summary",
        "description": "Active / achieved / abandoned goals (filterable by status, pillar).",
    },
    "nbhd_lessons_pending": {
        "render_block": "lesson_summary",
        "description": "Pending lesson approval queue (count + titles).",
    },
    "nbhd_journal_search": {
        "render_block": "journal_summary",
        "description": "Journal search results for a query string (recent entries first).",
    },
    "nbhd_calendar_list_events": {
        "render_block": "calendar_summary",
        "description": "Calendar events in a date range (Google Calendar primary).",
    },
}

_DEFAULT_MODEL = "haiku-4.5"
_TURN_TIMEOUT_SECONDS = 60


class DomainSummaryPayload(PatternPayload):
    """Schema for a domain-summary cron's typed_payload."""

    query_tool: str = Field(
        ...,
        description=(
            "Name of the read-only query tool to call at fire time. Must be "
            f"in the whitelist: {sorted(DOMAIN_SUMMARY_QUERY_TOOLS)}"
        ),
    )
    query_args: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments to pass to the query tool (tool-specific schema).",
    )
    render_block: str = Field(
        ...,
        description=(
            "The block type the outbound message must include — derived from "
            "the query_tool's whitelist entry. Stored explicitly to allow the "
            "validator to check it without re-deriving."
        ),
    )

    @field_validator("query_tool")
    @classmethod
    def _validate_query_tool(cls, value: str) -> str:
        if value not in DOMAIN_SUMMARY_QUERY_TOOLS:
            raise ValueError(f"query_tool must be one of {sorted(DOMAIN_SUMMARY_QUERY_TOOLS)}; got {value!r}")
        return value

    @model_validator(mode="after")
    def _validate_render_block_matches_tool(self) -> DomainSummaryPayload:
        expected = DOMAIN_SUMMARY_QUERY_TOOLS[self.query_tool]["render_block"]
        if self.render_block != expected:
            raise ValueError(
                f"render_block must be {expected!r} for query_tool={self.query_tool!r}; got {self.render_block!r}"
            )
        return self


class DomainSummaryHandler(PatternHandler):
    pattern = "domain_summary"
    payload_schema = DomainSummaryPayload

    def build_oc_data(
        self,
        payload: DomainSummaryPayload,
        *,
        tenant: Any,
        name: str,
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        tool_desc = DOMAIN_SUMMARY_QUERY_TOOLS[payload.query_tool]["description"]
        message = (
            "You are firing a scheduled domain summary. Steps:\n"
            f"1. Call `{payload.query_tool}` with the args provided below. "
            f"({tool_desc})\n"
            "2. Render the result as a concise, scannable summary the user can "
            "read on mobile. Lead with the key metric/count.\n"
            "3. Call `nbhd_send_to_user` exactly once with that summary. The "
            f"first line of your message MUST include the literal marker "
            f"`[block: {payload.render_block}]` so downstream tooling can "
            "identify the render type.\n"
            "4. Do not create tasks, goals, or follow-up crons during this "
            "turn — your job is to summarize, not to mutate state.\n\n"
            f"QUERY ARGS:\n{payload.query_args}"
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

    def get_tools_allow(self, payload: DomainSummaryPayload) -> list[str]:
        return ["nbhd_send_to_user", payload.query_tool]

    def get_prompt_injection(
        self,
        payload: DomainSummaryPayload,
        *,
        tenant: Any,
        name: str,
    ) -> str:
        return (
            "## Cron pattern: domain_summary\n"
            f"This turn renders a `{payload.render_block}` summary. Call "
            f"the query tool, then send a concise summary including the "
            f"marker `[block: {payload.render_block}]` on the first line. "
            "Do not mutate any state."
        )

    def validate_outbound_message(
        self,
        content: str,
        payload: DomainSummaryPayload,
    ) -> tuple[bool, str | None]:
        marker = f"[block: {payload.render_block}]"
        if marker in (content or ""):
            return True, None
        return False, (
            f"Outbound message must include the render marker {marker!r} "
            f"on the first line. Got: {(content or '')[:200]!r}"
        )


register_handler(DomainSummaryHandler())
