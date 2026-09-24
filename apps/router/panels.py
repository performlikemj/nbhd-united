"""Assistant-attached live references. No observations or snapshots belong here.

Generate the runtime tool schema with ``python -m apps.router.panels``; the
checked-in JS is derived from these models and pinned by a parity test.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, RootModel, ValidationError, model_validator

from apps.router.chat_gates import chat_panels_tool_enabled, chat_shape_enabled

logger = logging.getLogger(__name__)

PANEL_KINDS = ("sleep", "schedule", "training_week", "workout", "timer", "journal_table", "tasks", "log_table")
PanelKind = Literal[*PANEL_KINDS]
PANEL_RANGES = ("last_night", "today", "yesterday", "tomorrow", "this_week", "last_week", "this_month", "last_month")
TASK_FILTERS = ("due_today", "overdue", "open", "this_week")
LOG_METRICS = ("body_weight", "sleep")
MAX_PANELS = 6


def _iso_day(value: str) -> str:
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("expected ISO calendar date")
    return value


class PanelParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Omission is allowed; explicit null is not. Defaults are excluded on output.
    range: Literal[*PANEL_RANGES] = None
    day: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_iso_day)] = None
    metric: Literal[*LOG_METRICS] = Field(default=None, description="log_table only")
    filter: Literal[*TASK_FILTERS] = Field(default=None, description="tasks only")
    duration_seconds: int = Field(default=None, ge=1, le=14400, description="timer only")


class Panel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: PanelKind
    params: PanelParams = Field(default_factory=PanelParams)
    title: str = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def scoped_params(self):
        for key, kind in (("metric", "log_table"), ("filter", "tasks"), ("duration_seconds", "timer")):
            if key in self.params.model_fields_set and self.kind != kind:
                raise ValueError("parameter is not valid for panel kind")
        return self


class PanelList(RootModel[list[Panel]]):
    root: list[Panel] = Field(max_length=MAX_PANELS)


class _PanelCandidates(RootModel[list[Any]]):
    """Decode the envelope first so one bad candidate never loses its siblings."""

    model_config = ConfigDict(strict=True)


def validate_panels(value: Any) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        logger.warning("panels_dropped reason=not_list")
        return []
    valid = []
    for candidate in value:
        try:
            panel = Panel.model_validate(candidate)
        except ValidationError:
            logger.warning("panel_dropped reason=invalid_schema")
            continue
        if len(valid) == MAX_PANELS:
            logger.warning("panels_dropped reason=limit")
            break
        valid.append(panel.model_dump(mode="json", exclude_none=True))
    return valid


def extract_panels(text: str) -> tuple[str, list[dict]]:
    """Strip every nbhd-panels fence, including malformed/unclosed fences.

    The first list with valid references (or an explicit empty list) wins.
    Scan fence delimiters only; Pydantic parses and validates the JSON payload.
    Text without a panel fence is returned byte-for-byte unchanged.
    """
    kept, payload = [], []
    inside = found = False
    winner = None
    for line in text.splitlines(keepends=True):
        marker = line.strip()
        if not inside and marker == "```nbhd-panels":
            inside = found = True
            payload = []
        elif inside and marker == "```":
            inside = False
            try:
                candidates = _PanelCandidates.model_validate_json("".join(payload)).root
            except ValidationError:
                logger.warning("panels_dropped reason=malformed_block")
                continue
            panels = validate_panels(candidates)
            if winner is None and (panels or not candidates):
                winner = panels
        elif inside:
            payload.append(line)
        else:
            kept.append(line)
    if inside:
        logger.warning("panels_dropped reason=unclosed_block")
    return ("".join(kept).strip() if found else text), winner or []


def strip_streaming_panels(text: str) -> str:
    """Hide fences and trailing opener prefixes until cumulative text disambiguates."""
    kept = []
    inside = found = False
    ordinary_fence = False
    lines = text.splitlines(keepends=True)
    opener = "```nbhd-panels"
    for index, line in enumerate(lines):
        marker = line.strip()
        trailing_prefix = (
            index == len(lines) - 1 and not line.endswith(("\n", "\r")) and bool(marker) and opener.startswith(marker)
        )
        if not inside and not ordinary_fence and (marker == opener or trailing_prefix):
            inside = found = True
        elif inside and marker == "```":
            inside = False
        elif not inside:
            kept.append(line)
            if marker == "```":
                ordinary_fence = not ordinary_fence
            elif marker.startswith("```") and not ordinary_fence:
                ordinary_fence = True
    return "".join(kept).rstrip() if found else text


def prepare_panels(tenant, value, *, tool: bool = False) -> list[dict]:
    """Gate the originating surface and keep titles in placeholder space at rest.

    Proactive/tool writers must pass tool=True; ordinary chat uses the shape gate.
    """
    from apps.pii.authoring import truncate_placeholder_safe
    from apps.pii.egress import redact_known_values

    enabled = chat_panels_tool_enabled(tenant) if tool else chat_shape_enabled(tenant)
    if not enabled:
        if value:
            logger.warning("panels_dropped reason=tenant_disabled")
        return []
    panels = validate_panels(value)
    for panel in panels:
        if "title" in panel:
            panel["title"] = truncate_placeholder_safe(
                redact_known_values(tenant, panel["title"], seam="panel_storage"), 60
            )
    return panels


def rehydrate_panels(value, entity_map, *, tenant_id) -> list[dict]:
    from apps.router.reply_text import finalize_outbound_text

    panels = validate_panels(value)
    for panel in panels:
        if "title" in panel:
            panel["title"] = finalize_outbound_text(
                panel["title"], entity_map, tenant_id=tenant_id, channel="panel_title"
            )[:60]
    return panels


PANEL_VOCABULARY = (
    f"kinds={','.join(PANEL_KINDS)}; optional params: range={','.join(PANEL_RANGES)}, "
    f"day=YYYY-MM-DD, metric={','.join(LOG_METRICS)} (log_table only), "
    f"filter={','.join(TASK_FILTERS)} (tasks only), duration_seconds=1..14400 (timer only)"
)
CHAT_PANEL_INSTRUCTION = (
    "For useful live cards, end the reply with ONE fenced `nbhd-panels` JSON list of "
    "{kind,params,title?} references (max 6; title <=60 chars; no snapshots); " + PANEL_VOCABULARY + ".\n"
)
MORNING_PANEL_INSTRUCTION = (
    "In that same single nbhd_send_to_user call, attach panels only where fresh tool results show relevant data: "
    'sleep {"range":"last_night"}, workout {"day":"<today in the user timezone, YYYY-MM-DD>"}, '
    'schedule {"range":"today"}, tasks {"filter":"due_today"}, '
    'log_table {"metric":"body_weight","range":"this_month"}. '
    "Read nbhd_fuel_summary for sleep, workouts and weight; use current task/calendar results. "
    "Omit cards without evidence. Keep prose short; live editable cards carry detail. "
    "Panels are references, never snapshots (max 6; optional title <=60 chars); " + PANEL_VOCABULARY + "."
)


def tool_schema_module() -> str:
    schema = PanelList.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, list):
            return [inline(item) for item in node]
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(definitions[node["$ref"].rsplit("/", 1)[-1]])
            return {key: inline(value) for key, value in node.items()}
        return node

    return (
        "// Generated by python -m apps.router.panels; do not edit by hand.\n"
        "export const panelSchema = " + json.dumps(inline(schema), indent=2) + ";\n"
    )


if __name__ == "__main__":
    print(tool_schema_module(), end="")
