"""Cardio materialisation and content-free adoption telemetry."""

from math import ceil

from .set_contract import normalize_detail


def materialize_prescription(fields, *, category=None, stored_detail=None, stored_duration=None):
    """Return a write patch, retaining omission and clearing stale derived duration.

    Only duration explicitly in this write may override segment totals. A bare
    duration edit reuses stored segments; an unrelated edit leaves them alone.
    """
    out = dict(fields)
    category = out.get("category", category or "other")
    old = stored_detail if isinstance(stored_detail, dict) else {}
    if "detail_json" not in out:
        if "duration_minutes" not in out or "segments" not in old:
            return out
        out["detail_json"] = old
    detail = out["detail_json"]
    if not isinstance(detail, dict):
        return out
    explicit = out.get("duration_minutes")
    if explicit is not None:
        try:
            explicit = int(explicit)
        except (ValueError, TypeError):
            explicit = None
    detail, normalized_category = normalize_detail(
        detail, category, activity=out.get("activity"), explicit_duration_minutes=explicit
    )[:2]
    out["detail_json"] = detail
    if normalized_category != category:
        out["category"] = normalized_category
    if "segments" in detail and category == "cardio":
        seconds = detail["planned"].get("duration_s")
        out["duration_minutes"] = (
            explicit if explicit is not None else ceil(seconds / 60) if seconds is not None else None
        )
    elif "segments" in old and "duration_minutes" not in out:
        seconds = (old.get("planned") or {}).get("duration_s")
        if seconds is not None and stored_duration == ceil(seconds / 60):
            out["duration_minutes"] = None
    return out


def prescription_shape(detail):
    detail = detail if isinstance(detail, dict) else {}
    if detail.get("segments"):
        return "segments"
    if detail.get("structure"):
        return "structure"
    if detail.get("exercises"):
        return "exercises"
    return "flat"


def emit_prescription_shape(tenant, category, detail):
    if category != "cardio":
        return
    from apps.platform_logs.telemetry import emit_tool_event

    # Each event is one counter increment; reason_code is its bounded value.
    emit_tool_event(
        namespace="fuel",
        tool_name="fuel.cardio.prescription_shape",
        tenant_id=getattr(tenant, "id", None),
        outcome="accepted",
        reason_code=prescription_shape(detail),
        detail={"category": "cardio"},
    )
