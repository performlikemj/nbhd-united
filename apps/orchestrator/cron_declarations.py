"""Fail closed on declarations the pinned operator adapter cannot preserve."""

import json
from pathlib import Path

FIELDS = json.loads(Path(__file__).with_name("cron-declaration-fields.json").read_text())


def declaration_fields(job):
    """Discard only proven read metadata and the CLI-generated default policy.

    A different scheduled tool policy is an authored execution requirement,
    not metadata: leave it present so unknown-field validation refuses it.
    """
    if not isinstance(job, dict):
        return job
    job = {k: v for k, v in job.items() if k not in FIELDS["observation"]}
    policy = job.get("scheduledToolPolicy")
    if isinstance(policy, dict) and type(policy.get("version")) is int and policy == {"version": 1, "mode": "trusted"}:
        job.pop("scheduledToolPolicy")
    return job


def supported_declaration(job):
    job = declaration_fields(job)
    if not isinstance(job, dict) or set(job) - set(FIELDS["job"]):
        return False
    for part in ("payload", "delivery", "schedule", "pacing"):
        value = job.get(part)
        if value is None:
            value = {}
        if not isinstance(value, dict) or set(value) - set(FIELDS[part]):
            return False
    payload, schedule, delivery = (job.get(k) or {} for k in ("payload", "schedule", "delivery"))
    if payload.get("kind") not in {"agentTurn", "systemEvent"}:
        return False
    if schedule.get("kind") not in {"at", "cron", "every"}:
        return False
    if delivery.get("mode", "none") not in {"none", "announce", "webhook"}:
        return False
    if delivery.get("mode") == "webhook" and (
        not str(delivery.get("to", "")).startswith(("https://", "http://"))
        or any(delivery.get(k) is not None for k in ("channel", "accountId", "threadId"))
    ):
        return False
    if payload.get("kind") == "systemEvent" and (
        set(payload) - {"kind", "text", "message", "event", "toolsAllow", "toolsAllowIsDefault"}
        or delivery.get("mode") == "announce"
    ):
        return False
    tools = payload.get("toolsAllow")
    if tools is not None and (
        not tools
        or not any(str(t).strip() for t in (tools if isinstance(tools, list) else str(tools).replace(",", " ").split()))
    ):
        return False
    if payload["kind"] == "agentTurn" and not str(payload.get("message", payload.get("text", ""))).strip():
        return False
    target = job.get("sessionTarget", "isolated" if payload["kind"] == "agentTurn" else "main")
    if not isinstance(target, str) or (
        target not in {"main", "isolated", "current"} and not (target.startswith("session:") and target[8:])
    ):
        return False
    if (target == "main") != (payload["kind"] == "systemEvent"):
        return False
    if job.get("wakeMode", "now") not in {"now", "next-heartbeat"}:
        return False
    if schedule["kind"] == "every":
        ms = schedule.get("everyMs")
        if (
            not isinstance(ms, (int, float))
            or ms < 1000
            or ms % 1000
            or set(schedule) - {"kind", "everyMs", "anchorMs"}
        ):
            return False
        anchor = schedule.get("anchorMs")
        if anchor is not None and (not isinstance(anchor, int) or isinstance(anchor, bool) or anchor < 0):
            return False
    elif schedule["kind"] == "cron":
        if not schedule.get("expr") or set(schedule) - {"kind", "expr", "tz", "staggerMs"}:
            return False
        ms = schedule.get("staggerMs", 0)
        if not isinstance(ms, (int, float)) or ms < 0 or ms % 1000:
            return False
    else:
        from .migration_preservation import authored_at_ms

        if authored_at_ms(job) is None or set(schedule) - {"kind", "at", "atMs", "expr", "tz"}:
            return False
    return True
