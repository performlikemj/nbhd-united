"""Conservative contract for the UNCHANGED signed selector and runtime writer.

This is migration-only. Never broaden the image's projection here: unknown
fields and controls are refusals, including on disabled declarations.
"""

import copy
import hashlib
import json
import re
from collections import Counter

from .cron_declarations import declaration_fields, supported_declaration


def preservation_reasons(job, *, managed=True):
    job = declaration_fields(job)
    reasons = set()
    try:
        supported = supported_declaration(job)
    except (TypeError, ValueError, AttributeError):
        supported = False
    if not supported:
        reasons.add("unsupported_declaration")
        return reasons
    schedule, payload, delivery = (job.get(k) or {} for k in ("schedule", "payload", "delivery"))
    from .cron_reconcile import _is_unmanaged_cron

    if payload.get("kind") == "systemEvent":
        # The unchanged writer always emits --no-deliver, and the real 9.4
        # CLI rejects that combination for both supplied session targets.
        reasons.add("system_event_no_deliver")
    if not job.get("enabled", True):
        reasons.add("disabled_not_projected")
    if schedule.get("kind") != "at" and (not managed or _is_unmanaged_cron(job.get("name", ""))):
        reasons.add("unmanaged_not_projected")
    if not isinstance(job.get("enabled", True), bool):
        reasons.add("unsupported_enabled")
    if schedule.get("kind") == "cron":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        from croniter import croniter

        if not isinstance(schedule.get("expr"), str) or not croniter.is_valid(schedule["expr"]):
            reasons.add("cron_invalid")
        try:
            ZoneInfo(schedule.get("tz") or "UTC")
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            reasons.add("timezone_invalid")
    for field in ("toolsAllow", "fallbacks"):
        values = payload.get(field)
        if isinstance(values, list) and (
            not values or any(not isinstance(v, str) or not v or re.search(r"[,\s]", v) for v in values)
        ):
            reasons.add("execution_list_projection")
    if payload.get("timeoutSeconds") is not None and (
        type(payload["timeoutSeconds"]) is not int or payload["timeoutSeconds"] < 1
    ):
        reasons.add("execution_timeout")
    if schedule.get("anchorMs") is not None:
        reasons.add("interval_anchor")
    if schedule.get("kind") == "cron" and (
        not isinstance(schedule.get("expr"), str) or len(schedule["expr"].split()) != 5
    ):
        reasons.add("cron_seconds_or_nonstandard")
    if schedule.get("staggerMs") is not None:
        reasons.add("cron_stagger")
    if delivery.get("mode", "none") not in {"none", "announce"}:
        reasons.add("delivery_mode")
    for field in ("to", "accountId", "threadId"):
        if delivery.get(field) not in (None, ""):
            reasons.add("delivery_" + field)
    if delivery.get("channel") not in (None, "", "last") and delivery.get("mode") != "announce":
        reasons.add("delivery_channel")
    if delivery.get("bestEffort"):
        reasons.add("delivery_best_effort")
    default_session = "isolated" if payload.get("kind") == "agentTurn" else "main"
    if job.get("sessionTarget", default_session) != default_session or job.get("sessionKey") is not None:
        reasons.add("execution_session")
    if job.get("pacing") or payload.get("thinking") is not None:
        reasons.add("execution_controls")
    if job.get("deleteAfterRun", schedule.get("kind") == "at") != (schedule.get("kind") == "at"):
        reasons.add("execution_retention")
    if job.get("displayName") not in (None, "", job.get("name")):
        reasons.add("display_name")
    if payload.get("kind") == "systemEvent" and payload.get("toolsAllow") not in (None, ["*"]):
        reasons.add("execution_tools")
    # Match the unchanged writer's key-independent safety gate, including JSON
    # embedded in a message. Do not print the matching text or field values.
    if re.search(
        r'"(?:command|commandArgv|command_argv|commandInput|commandCwd|commandEnv|script)"\s*:', json.dumps(job), re.I
    ):
        reasons.add("writer_safety_refusal")
    return reasons


def reason_counts(declarations):
    counts = Counter()
    for job, managed in declarations:
        counts.update(preservation_reasons(job, managed=managed))
    return dict(sorted(counts.items()))


def normalized_declaration(job, *, pins=None):
    """Semantic JSON contract mirrored in migration_cron_digest.mjs.

    Runtime-generated anchors/staggers are ignored only when canonical truth
    does not pin them. Payload and destination fields always participate.
    """
    s, p, d = (copy.deepcopy(job.get(k) or {}) for k in ("schedule", "payload", "delivery"))
    pins = {k: k in s for k in ("anchorMs", "staggerMs")} if pins is None else pins
    for k in ("anchorMs", "staggerMs"):
        if not pins.get(k):
            s.pop(k, None)
    if s.get("kind") == "at":
        from apps.cron.pending_at_views import _at_fires_at_ms

        due = _at_fires_at_ms(job)
        s.pop("at", None)
        s["atMs"] = due
        s.pop("tz", None)  # An ISO one-shot is an instant, independent of display zone.
    if s.get("kind") == "cron":
        s["tz"] = s.get("tz") or "UTC"
    if p.get("kind") == "agentTurn":
        p["message"] = p.get("message", p.get("text", ""))
        p.pop("text", None)
    else:
        p["text"] = p.get("text", p.get("message", p.get("event", "heartbeat")))
        p.pop("message", None)
        p.pop("event", None)
    p.pop("toolsAllowIsDefault", None)
    p.setdefault("toolsAllow", ["*"])
    for k in ("toolsAllow", "fallbacks"):
        if isinstance(p.get(k), str):
            p[k] = [v for v in re.split(r"[,\s]+", p[k]) if v]
    p.setdefault("lightContext", False)
    for k, default in {
        "mode": "none",
        "channel": "last",
        "to": "",
        "accountId": "",
        "threadId": "",
        "bestEffort": False,
    }.items():
        d[k] = d.get(k) if d.get(k) is not None else default
    d["channel"] = d.get("channel") or "last"
    return {
        "schedule": s,
        "payload": p,
        "delivery": d,
        "enabled": job.get("enabled", True),
        "sessionTarget": job.get("sessionTarget") or ("isolated" if p.get("kind") == "agentTurn" else "main"),
        "sessionKey": job.get("sessionKey") or "",
        "wakeMode": job.get("wakeMode") or "now",
        "deleteAfterRun": job.get("deleteAfterRun", s.get("kind") == "at"),
        "agentId": job.get("agentId") or "",
        "description": job.get("description") or "",
        "pacing": job.get("pacing") or {},
    }


def declaration_digest(job, *, pins=None):
    encoded = json.dumps(
        normalized_declaration(job, pins=pins), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def canonical_digests(jobs):
    return {
        j["declarationKey"]: {
            "digest": declaration_digest(j),
            "pins": {k: k in (j.get("schedule") or {}) for k in ("anchorMs", "staggerMs")},
        }
        for j in jobs
    }


def observed_declaration(job, canonical):
    """Unpinned timing generated by 9.4 is not an authored requirement.

    Only verify-only uses this with an exact canonical declaration-key match.
    Legacy source capture retains conservative timing preservation checks.
    """
    result = copy.deepcopy(job)
    if canonical is not None:
        schedule = result.get("schedule") or {}
        authored = canonical.get("schedule") or {}
        if schedule.get("kind") == authored.get("kind"):
            for field in ("anchorMs", "staggerMs"):
                if field not in authored:
                    schedule.pop(field, None)
    return result


def writer_stable_declaration(job):
    """Equivalent ISO form echoed by the real CLI and stable in the writer."""
    from datetime import UTC, datetime

    from apps.cron.pending_at_views import _at_fires_at_ms

    result = copy.deepcopy(job)
    schedule = result.get("schedule") or {}
    if schedule.get("kind") == "at":
        due = _at_fires_at_ms(result)
        if due is not None:
            schedule["at"] = (
                datetime.fromtimestamp(due / 1000, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
            schedule.pop("atMs", None)
            schedule.pop("tz", None)
    return result
