"""Cron drift detection helpers — shared between the postgres-canonical
reconciler (``cron_reconcile.regenerate_tenant_crons``) and the seed-row
refresher (``services.refresh_system_cron_rows_from_seed``).

The reconciler diffs ``CronJob`` rows (postgres = desired) against the
gateway's ``cron.list`` output (OpenClaw runtime = existing). When fields
that the user can't customize drift, the reconciler does ``cron.remove``
+ ``cron.add`` to converge.

Pure functions only — no Django imports. The same helpers are used in
unit tests against synthetic job dicts.
"""

from __future__ import annotations

# Non-message payload fields. The user can't customize these via the
# dashboard, so any drift must trigger a recreate. ``model`` covers the
# canary-2026-05-12 case (stale ``anthropic-cli/...`` left over from BYO
# setup). ``kind`` covers the legacy systemEvent → agentTurn migration.
_PAYLOAD_NON_MESSAGE_DRIFT_FIELDS = ("model", "kind")

# Schedule fields the reconciler compares structurally. ``tz`` derives
# from ``tenant.user.timezone`` and is not separately user-customizable.
# ``expr`` is user-customizable via the dashboard but the source-of-truth
# lives in postgres, so any difference in OC vs postgres means push.
# ``kind`` is "cron" / "at" / "every" — payload-level shape.
_SCHEDULE_DRIFT_FIELDS = ("expr", "tz", "kind")


def strip_date_line(message: str) -> str:
    """Strip the leading ``Current date and time:`` preamble.

    ``_prepare_cron_prompt`` injects today's date at the top of every
    cron message, which means a naive existing-vs-desired compare would
    differ every day and trigger churn. We compare the structural body
    only.

    Stability of this comparison also depends on ``_build_cron_message``
    matching what OpenClaw stores back via ``cron.list``. OC strips
    trailing whitespace on store (``coercePayload`` → ``normalizeOptionalString``
    → ``value?.trim()``), so ``_build_cron_message`` calls ``.strip()`` on
    its output to mirror that. If a future OpenClaw bump adds more
    normalization (e.g. line-ending conversion, NFC unicode),
    ``project_openclaw_cron_payload_shape.md`` lists the audit step.
    """
    if not isinstance(message, str):
        return ""
    if not message.startswith("Current date and time:"):
        return message
    idx = message.find("\n\n")
    if idx == -1:
        return message
    return message[idx + 2 :]


def _resolved_model(job: dict) -> str | None:
    """Return the model string for a job, normalizing OC's top-level fold.

    OpenClaw normalizes a top-level ``job.model`` into ``payload.model`` on
    ``cron.add`` (observed on Heartbeat 2026-05-12 16:11). So Heartbeat's
    desired shape is ``{model: "openrouter/...", payload: {kind, message}}``
    but OC stores it back as ``{payload: {model: "openrouter/...", ...}}``.
    A naive ``payload.model`` compare would say DRIFT every time on
    Heartbeat. Reading both top-level and payload.model captures both
    representations.
    """
    if not isinstance(job, dict):
        return None
    payload = job.get("payload")
    if isinstance(payload, dict) and payload.get("model") is not None:
        return payload["model"]
    return job.get("model")


def payload_non_message_drift(existing: dict, desired: dict) -> list[str]:
    """Return non-message payload fields that drifted between sides.

    Uses the top-level ``model`` fallback for the desired side (Heartbeat).
    The existing side is read from ``payload`` only since OC always stores
    it there.
    """
    if not isinstance(existing, dict) or not isinstance(desired, dict):
        return []
    existing_payload = existing.get("payload", {})
    desired_payload = desired.get("payload", {})
    if not isinstance(existing_payload, dict) or not isinstance(desired_payload, dict):
        return []

    drift: list[str] = []
    for field in _PAYLOAD_NON_MESSAGE_DRIFT_FIELDS:
        existing_value = existing_payload.get(field)
        if field == "model":
            desired_value = _resolved_model(desired)
            existing_value = _resolved_model(existing)
        else:
            desired_value = desired_payload.get(field)
        if existing_value != desired_value:
            drift.append(field)
    return drift


def message_body_drift(existing: dict, desired: dict) -> bool:
    """Return True if the message body differs after stripping the date preamble.

    Both sides have the ``Current date and time: ...\\n\\n`` preamble
    prepended fresh on every render; comparing raw messages would churn
    daily. We compare the structural body.
    """
    if not isinstance(existing, dict) or not isinstance(desired, dict):
        return False
    existing_payload = existing.get("payload") or {}
    desired_payload = desired.get("payload") or {}
    existing_msg = existing_payload.get("message", "") if isinstance(existing_payload, dict) else ""
    desired_msg = desired_payload.get("message", "") if isinstance(desired_payload, dict) else ""
    return strip_date_line(existing_msg) != strip_date_line(desired_msg)


def schedule_drift(existing: dict, desired: dict) -> list[str]:
    """Return schedule fields that drifted (expr, tz, kind)."""
    if not isinstance(existing, dict) or not isinstance(desired, dict):
        return []
    existing_sched = existing.get("schedule") or {}
    desired_sched = desired.get("schedule") or {}
    if not isinstance(existing_sched, dict) or not isinstance(desired_sched, dict):
        return []
    return [f for f in _SCHEDULE_DRIFT_FIELDS if existing_sched.get(f) != desired_sched.get(f)]


def job_drift(existing: dict, desired: dict) -> list[str]:
    """Return all drifted dimensions between an existing OC job and a desired
    postgres-side job dict.

    Output is a flat list of dimension tags: ``["model"]``, ``["message"]``,
    ``["schedule.expr"]``, ``["enabled"]``, etc. Empty list means converged.
    Callers treat any non-empty result as "needs recreate".
    """
    drift: list[str] = list(payload_non_message_drift(existing, desired))
    if message_body_drift(existing, desired):
        drift.append("message")
    for field in schedule_drift(existing, desired):
        drift.append(f"schedule.{field}")
    if bool(existing.get("enabled", True)) != bool(desired.get("enabled", True)):
        drift.append("enabled")
    return drift
