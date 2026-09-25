"""Conservative contract for the UNCHANGED signed selector and runtime writer.

This is migration-only. Never broaden the image's projection here: unknown
fields and controls are refusals, including on disabled declarations.
"""

import copy
import hashlib
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from .cron_declarations import declaration_fields, supported_declaration

NORMALIZATION = json.loads(Path(__file__).with_name("cron-normalization.json").read_text())


def normalized_optionals(job):
    result = copy.deepcopy(job)
    if not isinstance(result, dict):
        return result
    for part, fields in NORMALIZATION["nullAbsent"].items():
        target = result if part == "root" else result.get(part)
        if isinstance(target, dict):
            for field in fields:
                if target.get(field) is None:
                    target.pop(field, None)
    # Mirror the signer (share_cron_sync._payload_model): an agentTurn's
    # top-level model pin travels as payload.model. A conflicting pair stays
    # unfolded, so the unknown root field refuses it.
    payload = result.get("payload")
    model = result.get("model")
    if (
        isinstance(model, str)
        and model
        and isinstance(payload, dict)
        and payload.get("kind") == "agentTurn"
        and payload.get("model") in (None, model)
    ):
        payload["model"] = result.pop("model")
    return result


def authored_at_ms(job):
    """An instant comes only from authored fields; conflicts fail closed."""
    from datetime import datetime

    schedule = job.get("schedule") or {}
    instants = []
    for field in NORMALIZATION["instantFields"]:
        value = schedule.get(field)
        if value is None:
            continue
        if type(value) is int and 0 <= value <= 8640000000000000:
            instant = value
        elif isinstance(value, str) and re.fullmatch(NORMALIZATION["instantPattern"], value):
            try:
                instant = int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                return None
        else:
            return None
        instants.append(instant)
    return instants[0] if instants and len(set(instants)) == 1 else None


def scalar_types_valid(job):
    """Reject wrong scalar types before falsy defaults/aliases can erase them."""
    types = {"boolean": bool, "integer": int, "string": str}
    if not isinstance(job, dict):
        return False
    for part, fields in NORMALIZATION["scalarTypes"].items():
        target = job if part == "root" else job.get(part, {})
        if not isinstance(target, dict):
            return False
        for field, kind in fields.items():
            if field in target:
                if type(target[field]) is not types[kind]:
                    return False
                if kind == "integer" and abs(target[field]) > 2**53 - 1:
                    return False
    return True


def preservation_reasons(job, *, managed=True):
    job = normalized_optionals(declaration_fields(job))
    reasons = set()
    try:
        supported = scalar_types_valid(job) and supported_declaration(job)
    except (TypeError, ValueError, AttributeError):
        supported = False
    if not supported:
        reasons.add("unsupported_declaration")
        return reasons
    schedule, payload, delivery = (job.get(k) or {} for k in ("schedule", "payload", "delivery"))
    aliases = NORMALIZATION["aliases"].get(payload.get("kind"), {}).get("sources", [])
    authored_texts = [payload[k] for k in aliases if payload.get(k) is not None]
    if authored_texts and any(value != authored_texts[0] for value in authored_texts):
        reasons.add("conflicting_text_aliases")
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
    if not reasons:
        if json_type_key(declaration_shape(job)) not in {json_type_key(shape) for shape in proven_shapes()}:
            reasons.add("unproven_shape")
    return reasons


def json_type_key(value):
    """Type-tagged JSON tree: Python's bool/int/float equality is unsafe here."""
    if isinstance(value, dict):
        return ("object", tuple((k, json_type_key(v)) for k, v in sorted(value.items())))
    if isinstance(value, list):
        return ("array", tuple(json_type_key(v) for v in value))
    return (type(value).__name__, value)


def declaration_shape(job, *, pins=None):
    """Exact normalized control combination; only data slots vary by type.

    Models, tools, list order, timeout, flags and enum values remain literal.
    A fixture for one combination never admits a Cartesian product of controls.
    """
    result = normalized_declaration(job, pins=pins)
    for path in NORMALIZATION["shapeVariables"]:
        parts = path.split(".")
        target = result
        for part in parts[:-1]:
            target = target.get(part, {})
        key = parts[-1]
        if key in target and target[key] != "":
            value = target[key]
            if isinstance(value, str):
                target[key] = {"valueType": "string"}
            elif type(value) is int:
                target[key] = {"valueType": "integer"}
            else:
                target[key] = {"valueType": "unsupported"}
    return result


def proven_shapes():
    return _proven_shapes()


@lru_cache(maxsize=1)
def _proven_shapes():
    evidence = json.loads(Path(__file__).with_name("cron-proven-shapes.json").read_text())
    return [entry["shape"] for entry in evidence["shapes"]]


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
    job = normalized_optionals(job)
    s, p, d = (copy.deepcopy(job.get(k) or {}) for k in ("schedule", "payload", "delivery"))
    pins = {k: k in s for k in NORMALIZATION["timingPins"]} if pins is None else pins
    for k in NORMALIZATION["timingPins"]:
        if not pins.get(k):
            s.pop(k, None)
    if s.get("kind") == "at":
        due = authored_at_ms(job)
        for field in NORMALIZATION["instantFields"]:
            s.pop(field, None)
        s["atMs"] = due
        s.pop("tz", None)
    if s.get("kind") == "cron":
        s["tz"] = s.get("tz") or "UTC"
    alias = NORMALIZATION["aliases"].get(p.get("kind"))
    if alias:
        value = next((p[k] for k in alias["sources"] if p.get(k) is not None), alias["default"])
        for k in alias["sources"]:
            p.pop(k, None)
        p[alias["target"]] = value
    for k in NORMALIZATION["payloadObservations"]:
        p.pop(k, None)
    for part, target in (("payload", p), ("delivery", d)):
        for key, default in NORMALIZATION["defaults"][part].items():
            target.setdefault(key, copy.deepcopy(default))
    for k in NORMALIZATION["lists"]:
        if isinstance(p.get(k), str):
            p[k] = [v for v in re.split(r"[,\s]+", p[k]) if v]
    d["channel"] = d.get("channel") or "last"
    result = {k: copy.deepcopy(job.get(k, v)) for k, v in NORMALIZATION["defaults"]["root"].items()}
    return {
        **result,
        "schedule": s,
        "payload": p,
        "delivery": d,
        "sessionTarget": job.get("sessionTarget") or ("isolated" if p.get("kind") == "agentTurn" else "main"),
        "deleteAfterRun": job.get("deleteAfterRun", s.get("kind") == "at"),
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
            "pins": {k: (j.get("schedule") or {}).get(k) is not None for k in NORMALIZATION["timingPins"]},
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
                if authored.get(field) is None:
                    schedule.pop(field, None)
    return result


def writer_stable_declaration(job):
    """Equivalent ISO form echoed by the real CLI and stable in the writer."""
    from datetime import UTC, datetime

    result = copy.deepcopy(job)
    schedule = result.get("schedule") or {}
    if schedule.get("kind") == "at":
        due = authored_at_ms(result)
        if due is not None:
            schedule["at"] = (
                datetime.fromtimestamp(due / 1000, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
            schedule.pop("atMs", None)
            schedule.pop("expr", None)
            schedule.pop("tz", None)
    return result
