"""`task_hygiene` pattern: weekly proactive task-list cleanup, system-defined.

Like ``daily_briefing``, this is a system pattern — the agent's
``nbhd_cron_create_*`` tools do not accept it. Seeded by
``apps/cron/services.py::seed_task_hygiene_cron`` behind the
``TASK_HYGIENE_TENANT_IDS`` canary gate.

Why this pattern exists separately from ``daily_briefing``: the briefing's
``get_tools_allow`` is a deliberate *no-mutations* guard (the structural fix
for the 22:07 cron-creates-duplicate-task cascade) and must not be weakened.
Proactive cleanup genuinely needs to close and defer things, so it gets its
own pattern with its own, narrower mutation budget rather than loosening the
briefing's.

The mutation budget is the whole design:

  ALLOWED   nbhd_task_complete / nbhd_task_skip / nbhd_task_defer — lifecycle
            transitions on tasks that ALREADY exist. Every one is reversible
            by the user in conversation, and none can invent an item.

  ABSENT    nbhd_task_create / nbhd_task_update / nbhd_task_delete,
            every goal mutation, every finance/fuel/document/cron write.
            The turn cannot fabricate work, rewrite titles, or destroy
            anything.

Deletion is PROPOSED, never performed. ``nbhd_task_delete`` (PR 1) is
structurally two-phase: a first call returns ``confirmation_required`` and only
a follow-up carrying ``confirm=true`` deletes. That handshake is designed to be
completed by a human saying yes in conversation — a scheduled turn has no user
present to ask, so it could only ever satisfy the handshake by confirming to
itself. Leaving the tool out of ``toolsAllow`` makes that impossible rather
than merely discouraged; the summary lists candidates and the user confirms
them in chat on their own time.

Outbound contract mirrors the briefing's: marker check with
``revise_then_allow`` (max 1 revision), so a missing marker costs one retry and
then ships anyway — a hygiene summary is worth more late than never.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from apps.billing.constants import DEEPSEEK_FLASH_MODEL

from . import register_handler
from .base import PatternHandler, PatternPayload

# Same allowlist-safe model every other typed pattern pins — see the long
# rationale in pure_reminder.py / daily_briefing.py. A platform-initiated turn
# whose model sits outside the tenant's agents.defaults.models allowlist is
# rejected by OpenClaw's cron preflight before any tool runs, so the whole
# hygiene pass would silently never happen.
_CRON_MODEL = DEEPSEEK_FLASH_MODEL

# Longer than the briefing's 90s: this turn reads the task list, may fetch
# individual tasks for detail, performs several lifecycle calls, and only then
# composes a summary.
_TURN_TIMEOUT_SECONDS = 120

_MARKER = "[block: task_hygiene]"

# Read-only context tools.
_HYGIENE_QUERY_TOOLS: tuple[str, ...] = (
    "nbhd_task_list",
    "nbhd_task_get",
    "nbhd_goal_list",
    "nbhd_current_status",
)

# The three permitted lifecycle transitions. Deliberately NOT create/update/
# delete — see the module docstring. This tuple is pinned exactly by
# ``TaskHygieneTests.test_tools_allow_is_pinned_exactly``: adding a tool here
# without a matching review is the failure mode that test exists to catch.
_HYGIENE_LIFECYCLE_TOOLS: tuple[str, ...] = (
    "nbhd_task_complete",
    "nbhd_task_skip",
    "nbhd_task_defer",
)


class TaskHygienePayload(PatternPayload):
    """Schema for a task-hygiene cron's typed_payload.

    Carries thresholds only — never task content. Facts come from fresh tool
    calls at fire time, and free text is barred by ``extra="forbid"`` on the
    base class (cron prompts bypass inbound PII redaction, so a payload that
    accepted prose would be a leak surface — the workout_congrats lesson).
    """

    stale_after_days: int = Field(
        default=14,
        ge=1,
        le=365,
        description="An open task untouched for this many days counts as stale.",
    )


class TaskHygieneHandler(PatternHandler):
    pattern = "task_hygiene"
    payload_schema = TaskHygienePayload

    def build_oc_data(
        self,
        payload: TaskHygienePayload,
        *,
        tenant: Any,
        name: str,
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        message = (
            "You are running the weekly task-hygiene pass. Nobody asked for "
            "this and nobody is waiting on it — the user is not present in "
            "this turn. Work quietly and only interrupt them if you actually "
            "changed something or need a decision.\n"
            "\n"
            "STEP 1 — LOOK. Call `nbhd_task_list` for the current open and "
            "in-progress tasks. Use `nbhd_current_status` for the wider "
            "picture and `nbhd_goal_list` to see which goals are still live. "
            "Call `nbhd_task_get` on any individual task whose state you need "
            "detail on. Every judgement you make below must trace to one of "
            "those tool results — never to memory or assumption.\n"
            "\n"
            "STEP 2 — CLOSE what is already done. If the record shows the work "
            "happened (a completed session, a paid bill, a goal the task fed "
            "into that is already achieved), call `nbhd_task_complete`. If you "
            "are not sure it happened, do NOT complete it — propose it in the "
            "summary instead.\n"
            "\n"
            f"STEP 3 — TRIAGE what has gone stale (untouched for more than "
            f"{payload.stale_after_days} days). For each one pick ONE:\n"
            "  - `nbhd_task_defer` when it still matters but not now — push it "
            "to a realistic date.\n"
            "  - `nbhd_task_skip` when the moment for it has passed and doing "
            "it no longer makes sense.\n"
            "  - leave it alone when neither is clearly right.\n"
            "Give every change a one-line reason in the summary. Be "
            "conservative: a wrongly-skipped task is worse than a stale one.\n"
            "\n"
            "STEP 4 — PROPOSE deletions, never perform them. Some items are "
            "not stale but junk: test entries, exact duplicates, fragments "
            "that were never real tasks. You CANNOT delete them here. "
            "`nbhd_task_delete` is not available in this turn, and by design: "
            "deleting a task is permanent, cascades to its subtasks, and "
            "requires the user's explicit spoken confirmation in conversation "
            "before it goes through. This is a scheduled turn with no user in "
            "it, so that confirmation cannot be obtained — you would only be "
            "confirming to yourself. List the candidates in your summary "
            "instead, with a short reason each, and let the user confirm them "
            "in chat whenever they get to it.\n"
            "\n"
            "STEP 5 — REPORT, at most once. If you changed nothing AND have no "
            "deletion candidates, send NOTHING AT ALL and end the turn — a "
            "message saying 'everything looks fine' is noise the user did not "
            "ask for. Otherwise call `nbhd_send_to_user` EXACTLY ONCE with a "
            "short, scannable summary: what you closed, what you deferred or "
            "skipped and why, then the deletion candidates as a list the user "
            "can say yes or no to. Do not send a second message. Do not "
            "narrate your tool calls.\n"
            "\n"
            f"The first line of that message must include the literal marker "
            f"`{_MARKER}` so downstream tooling can identify the render type."
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
                "toolsAllow": self.get_tools_allow(payload),
                "timeoutSeconds": _TURN_TIMEOUT_SECONDS,
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        }

    def get_tools_allow(self, payload: TaskHygienePayload) -> list[str]:
        # The structural guard. Prose in the prompt can drift under a model
        # swap; this list cannot — the runtime simply has no create, update or
        # delete tool to call. Pinned exactly by the drift test.
        return [
            *_HYGIENE_QUERY_TOOLS,
            *_HYGIENE_LIFECYCLE_TOOLS,
            "nbhd_send_to_user",
        ]

    def get_outbound_contract(
        self,
        payload: TaskHygienePayload,
        *,
        name: str,
    ) -> dict[str, Any]:
        return {
            "check": {"kind": "marker", "marker": _MARKER},
            "on_fail": {"action": "revise_then_allow", "max_revisions": 1},
        }

    def validate_outbound_message(
        self,
        content: str,
        payload: TaskHygienePayload,
    ) -> tuple[bool, str | None]:
        if _MARKER in (content or ""):
            return True, None
        return False, (f"Task-hygiene outbound must include the marker {_MARKER!r}. Got: {(content or '')[:200]!r}")


register_handler(TaskHygieneHandler())
