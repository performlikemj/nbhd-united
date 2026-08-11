"""Canonical item normalization and manifest hashing for datebook sync v1."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils.dateparse import parse_date, parse_datetime

from .models import AuthorizationStatus, DueKind, SourceType, TimeKind

SOURCE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
HASH_RE = SOURCE_KEY_RE

TITLE_MAX = 256
LOCATION_MAX = 512
NOTES_MAX = 4000
IDENTIFIER_MAX = 255
FINGERPRINT_MAX = 64


@dataclass(frozen=True)
class ItemValidationError(ValueError):
    code: str

    def __str__(self) -> str:
        return self.code


def _fail(code: str):
    raise ItemValidationError(code)


def _reject_floats(value) -> None:
    if isinstance(value, float):
        _fail("floats_not_allowed")
    if isinstance(value, dict):
        for child in value.values():
            _reject_floats(child)
    elif isinstance(value, list):
        for child in value:
            _reject_floats(child)


def normalize_text(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _fail("invalid_text")
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    return normalized[:limit]


def _bounded_identifier(value, *, required: bool = False, limit: int = IDENTIFIER_MAX) -> str:
    normalized = normalize_text(value, limit).strip()
    if required and not normalized:
        _fail("missing_identifier")
    return normalized


def _date(value, code: str):
    if not isinstance(value, str):
        _fail(code)
    try:
        parsed = parse_date(value)
    except ValueError:
        parsed = None
    if parsed is None:
        _fail(code)
    return parsed


def _datetime(value, code: str, *, aware: bool) -> datetime:
    if not isinstance(value, str):
        _fail(code)
    try:
        parsed = parse_datetime(value)
    except ValueError:
        parsed = None
    if parsed is None or (parsed.tzinfo is not None) is not aware:
        _fail(code)
    return parsed


def _utc_seconds(value, code: str) -> str:
    parsed = _datetime(value, code, aware=True)
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _local_seconds(value, code: str) -> str:
    parsed = _datetime(value, code, aware=False)
    return parsed.replace(microsecond=0).isoformat(timespec="seconds")


def _tz_id(value, code: str) -> str:
    name = _bounded_identifier(value, required=True, limit=63)
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        _fail(code)
    return name


def canonical_json_bytes(value) -> bytes:
    _reject_floats(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _common(item: dict, *, calendar_field: str) -> tuple[dict, dict]:
    if not isinstance(item, dict):
        _fail("item_not_object")
    _reject_floats(item)

    source_key = item.get("source_key")
    if not isinstance(source_key, str) or SOURCE_KEY_RE.fullmatch(source_key) is None:
        _fail("invalid_source_key")
    source_type = item.get("source_type")
    if source_type not in SourceType.values:
        _fail("invalid_source_type")
    authorization = item.get("authorization_status")
    if authorization not in AuthorizationStatus.values:
        _fail("invalid_authorization_status")
    read_only = item.get("is_read_only")
    if not isinstance(read_only, bool):
        _fail("invalid_read_only")

    stage = {
        "source_key": source_key,
        "external_id": _bounded_identifier(item.get("external_id")),
        "series_id": _bounded_identifier(item.get("series_id")),
        "source_fingerprint": _bounded_identifier(
            item.get("source_fingerprint"),
            required=True,
            limit=FINGERPRINT_MAX,
        ),
        "source_type": source_type,
        "source_title": normalize_text(item.get("source_title"), TITLE_MAX),
        calendar_field: normalize_text(item.get(calendar_field), TITLE_MAX),
        "is_read_only": read_only,
        "authorization_status": authorization,
        "title": normalize_text(item.get("title"), TITLE_MAX),
        "location": normalize_text(item.get("location"), LOCATION_MAX),
        "notes": normalize_text(item.get("notes"), NOTES_MAX),
    }
    semantic = {
        "authorization_status": authorization,
        calendar_field: stage[calendar_field],
        "is_read_only": read_only,
        "location": stage["location"],
        "notes": stage["notes"],
        "source_fingerprint": stage["source_fingerprint"],
        "source_title": stage["source_title"],
        "source_type": source_type,
        "title": stage["title"],
    }
    return stage, semantic


def _event_time(item: dict) -> tuple[dict, dict]:
    value = item.get("time")
    if not isinstance(value, dict):
        _fail("invalid_event_time")
    kind = value.get("kind")
    if kind == TimeKind.ALL_DAY:
        start = _date(value.get("start_date"), "invalid_start_date")
        end = _date(value.get("end_date_exclusive"), "invalid_end_date")
        if end <= start:
            _fail("invalid_time_order")
        canonical = {
            "kind": kind,
            "start_date": start.isoformat(),
            "end_date_exclusive": end.isoformat(),
        }
        stage = {
            "time_kind": kind,
            "all_day_start_date": start.isoformat(),
            "all_day_end_date_exclusive": end.isoformat(),
            "zoned_start_at": None,
            "zoned_end_at": None,
            "tz_id": "",
            "floating_start_date": None,
            "floating_start_time": None,
            "floating_end_date": None,
            "floating_end_time": None,
        }
        return stage, canonical
    if kind == TimeKind.ZONED:
        start = _utc_seconds(value.get("start_at"), "invalid_start_at")
        end = _utc_seconds(value.get("end_at"), "invalid_end_at")
        if _datetime(end, "invalid_end_at", aware=True) <= _datetime(start, "invalid_start_at", aware=True):
            _fail("invalid_time_order")
        tz_id = _tz_id(value.get("tz_id"), "invalid_tz_id")
        canonical = {"kind": kind, "start_at": start, "end_at": end, "tz_id": tz_id}
        stage = {
            "time_kind": kind,
            "all_day_start_date": None,
            "all_day_end_date_exclusive": None,
            "zoned_start_at": start,
            "zoned_end_at": end,
            "tz_id": tz_id,
            "floating_start_date": None,
            "floating_start_time": None,
            "floating_end_date": None,
            "floating_end_time": None,
        }
        return stage, canonical
    if kind == TimeKind.FLOATING:
        start = _local_seconds(value.get("start_local"), "invalid_start_local")
        end = _local_seconds(value.get("end_local"), "invalid_end_local")
        if _datetime(end, "invalid_end_local", aware=False) <= _datetime(start, "invalid_start_local", aware=False):
            _fail("invalid_time_order")
        start_dt = _datetime(start, "invalid_start_local", aware=False)
        end_dt = _datetime(end, "invalid_end_local", aware=False)
        canonical = {"kind": kind, "start_local": start, "end_local": end}
        stage = {
            "time_kind": kind,
            "all_day_start_date": None,
            "all_day_end_date_exclusive": None,
            "zoned_start_at": None,
            "zoned_end_at": None,
            "tz_id": "",
            "floating_start_date": start_dt.date().isoformat(),
            "floating_start_time": start_dt.time().isoformat(timespec="seconds"),
            "floating_end_date": end_dt.date().isoformat(),
            "floating_end_time": end_dt.time().isoformat(timespec="seconds"),
        }
        return stage, canonical
    _fail("invalid_time_kind")


def _reminder_due(item: dict) -> tuple[dict, dict]:
    value = item.get("due")
    if not isinstance(value, dict):
        _fail("invalid_due")
    kind = value.get("kind")
    if kind == DueKind.NONE:
        return {
            "due_kind": kind,
            "due_date": None,
            "zoned_due_at": None,
            "due_tz_id": "",
            "floating_due_date": None,
            "floating_due_time": None,
        }, {"kind": kind}
    if kind == DueKind.ALL_DAY:
        due_date = _date(value.get("due_date"), "invalid_due_date")
        canonical = {"kind": kind, "due_date": due_date.isoformat()}
        return {
            "due_kind": kind,
            "due_date": due_date.isoformat(),
            "zoned_due_at": None,
            "due_tz_id": "",
            "floating_due_date": None,
            "floating_due_time": None,
        }, canonical
    if kind == DueKind.ZONED:
        due_at = _utc_seconds(value.get("due_at"), "invalid_due_at")
        tz_id = _tz_id(value.get("tz_id"), "invalid_due_tz_id")
        canonical = {"kind": kind, "due_at": due_at, "tz_id": tz_id}
        return {
            "due_kind": kind,
            "due_date": None,
            "zoned_due_at": due_at,
            "due_tz_id": tz_id,
            "floating_due_date": None,
            "floating_due_time": None,
        }, canonical
    if kind == DueKind.FLOATING:
        due_local = _local_seconds(value.get("due_local"), "invalid_due_local")
        due_dt = _datetime(due_local, "invalid_due_local", aware=False)
        canonical = {"kind": kind, "due_local": due_local}
        return {
            "due_kind": kind,
            "due_date": None,
            "zoned_due_at": None,
            "due_tz_id": "",
            "floating_due_date": due_dt.date().isoformat(),
            "floating_due_time": due_dt.time().isoformat(timespec="seconds"),
        }, canonical
    _fail("invalid_due_kind")


def _event_parts(item: dict) -> tuple[dict, dict]:
    stage, semantic = _common(item, calendar_field="calendar_title")
    recurring = item.get("is_recurring", False)
    if not isinstance(recurring, bool):
        _fail("invalid_is_recurring")
    time_stage, time_semantic = _event_time(item)
    stage.update(time_stage)
    stage["is_recurring"] = recurring
    semantic.update(
        {
            "is_recurring": recurring,
            "series_present": bool(stage["series_id"]),
            "time": time_semantic,
        }
    )
    return stage, semantic


def _reminder_parts(item: dict) -> tuple[dict, dict]:
    stage, semantic = _common(item, calendar_field="list_title")
    completed = item.get("completed")
    if not isinstance(completed, bool):
        _fail("invalid_completed")
    priority = item.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 9:
        _fail("invalid_priority")
    completed_at = None
    if completed:
        completed_at = _utc_seconds(item.get("completed_at"), "invalid_completed_at")
    elif item.get("completed_at") not in (None, ""):
        _fail("unexpected_completed_at")
    due_stage, due_semantic = _reminder_due(item)
    stage.update(due_stage)
    stage.update({"completed": completed, "completed_at": completed_at, "priority": priority})
    semantic.update(
        {
            "completed": completed,
            "completed_at": completed_at,
            "due": due_semantic,
            "priority": priority,
            "series_present": bool(stage["series_id"]),
        }
    )
    return stage, semantic


def content_hash_v1(entity_type: str, item: dict) -> str:
    if entity_type == "event":
        _stage, semantic = _event_parts(item)
    elif entity_type == "reminder":
        _stage, semantic = _reminder_parts(item)
    else:
        _fail("invalid_entity_type")
    return hashlib.sha256(canonical_json_bytes(semantic)).hexdigest()


def _clean(entity_type: str, item: dict) -> dict:
    if entity_type == "event":
        stage, semantic = _event_parts(item)
    else:
        stage, semantic = _reminder_parts(item)
    supplied = item.get("content_hash")
    expected = hashlib.sha256(canonical_json_bytes(semantic)).hexdigest()
    if not isinstance(supplied, str) or HASH_RE.fullmatch(supplied) is None:
        _fail("invalid_content_hash")
    if not hmac.compare_digest(supplied, expected):
        _fail("content_hash_mismatch")
    stage["content_hash"] = expected
    return stage


def clean_event_item(item: dict) -> dict:
    return _clean("event", item)


def clean_reminder_item(item: dict) -> dict:
    return _clean("reminder", item)


def manifest_digest_v1(items) -> str:
    canonical = [
        {"content_hash": content_hash, "source_key": source_key}
        for source_key, content_hash in sorted(items, key=lambda pair: pair[0])
    ]
    return hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()
