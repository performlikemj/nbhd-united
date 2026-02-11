"""Google provider API helpers for internal runtime endpoints."""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone
import html as html_lib
import re
from typing import Any

import httpx


def _google_get(
    url: str,
    access_token: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=20.0,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _google_post(
    url: str,
    access_token: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = httpx.post(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        json=payload,
        timeout=20.0,
    )
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, dict) else {}


def _header_value(headers: list[dict[str, Any]], name: str) -> str:
    expected = name.lower()
    for header in headers:
        if not isinstance(header, dict):
            continue
        header_name = str(header.get("name", "")).lower()
        if header_name == expected:
            return str(header.get("value", "")).strip()
    return ""


def _decode_gmail_part_data(data: str) -> str:
    raw = (data or "").strip()
    if not raw:
        return ""

    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
    except (binascii.Error, ValueError):
        return ""

    return decoded.decode("utf-8", errors="replace")


def _extract_gmail_body_parts(part: dict[str, Any]) -> tuple[list[str], list[str]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    mime_type = str(part.get("mimeType", "")).lower()
    body = part.get("body", {})
    body_data = body.get("data", "") if isinstance(body, dict) else ""
    decoded_body = _decode_gmail_part_data(str(body_data))

    if decoded_body:
        if mime_type.startswith("text/plain"):
            plain_parts.append(decoded_body)
        elif mime_type.startswith("text/html"):
            html_parts.append(decoded_body)

    subparts = part.get("parts", [])
    if isinstance(subparts, list):
        for subpart in subparts:
            if not isinstance(subpart, dict):
                continue
            child_plain, child_html = _extract_gmail_body_parts(subpart)
            plain_parts.extend(child_plain)
            html_parts.extend(child_html)

    return plain_parts, html_parts


def _strip_html(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html_lib.unescape(without_tags)).strip()


def _normalize_thread_message(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload", {})
    headers = payload.get("headers", []) if isinstance(payload, dict) else []
    if not isinstance(headers, list):
        headers = []

    return {
        "id": str(message.get("id", "")).strip(),
        "snippet": str(message.get("snippet", "")).strip(),
        "subject": _header_value(headers, "Subject"),
        "from": _header_value(headers, "From"),
        "date": _header_value(headers, "Date"),
        "internal_date": str(message.get("internalDate", "")).strip(),
    }


def list_gmail_messages(
    access_token: str,
    query: str = "",
    max_results: int = 5,
) -> dict[str, Any]:
    """Return normalized Gmail message metadata for assistant use."""
    safe_max_results = max(1, min(max_results, 10))
    params: dict[str, Any] = {"maxResults": safe_max_results}
    if query.strip():
        params["q"] = query.strip()

    base_payload = _google_get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        access_token=access_token,
        params=params,
    )

    messages = base_payload.get("messages")
    if not isinstance(messages, list):
        messages = []

    normalized_messages: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("id", "")).strip()
        if not message_id:
            continue

        detail_payload = _google_get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            access_token=access_token,
            params={
                "format": "metadata",
                "metadataHeaders": ["Subject", "From", "Date"],
            },
        )
        payload = detail_payload.get("payload", {})
        headers = payload.get("headers", []) if isinstance(payload, dict) else []
        if not isinstance(headers, list):
            headers = []

        normalized_messages.append(
            {
                "id": message_id,
                "thread_id": str(detail_payload.get("threadId", "")).strip(),
                "snippet": str(detail_payload.get("snippet", "")).strip(),
                "subject": _header_value(headers, "Subject"),
                "from": _header_value(headers, "From"),
                "date": _header_value(headers, "Date"),
                "internal_date": str(detail_payload.get("internalDate", "")).strip(),
            }
        )

    result_size_estimate = base_payload.get("resultSizeEstimate", len(normalized_messages))
    if not isinstance(result_size_estimate, int):
        result_size_estimate = len(normalized_messages)

    return {
        "messages": normalized_messages,
        "result_size_estimate": result_size_estimate,
    }


def list_calendar_events(
    access_token: str,
    time_min: str | None = None,
    time_max: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Return normalized primary calendar events."""
    safe_max_results = max(1, min(max_results, 20))
    params: dict[str, Any] = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": safe_max_results,
        "timeMin": time_min or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if time_max:
        params["timeMax"] = time_max

    payload = _google_get(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        access_token=access_token,
        params=params,
    )

    items = payload.get("items")
    if not isinstance(items, list):
        items = []

    events: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        start = item.get("start", {})
        end = item.get("end", {})
        if not isinstance(start, dict):
            start = {}
        if not isinstance(end, dict):
            end = {}

        events.append(
            {
                "id": str(item.get("id", "")).strip(),
                "summary": str(item.get("summary", "")).strip(),
                "status": str(item.get("status", "")).strip(),
                "html_link": str(item.get("htmlLink", "")).strip(),
                "start": start,
                "end": end,
            }
        )

    return {
        "events": events,
        "next_page_token": str(payload.get("nextPageToken", "")).strip(),
    }


def get_gmail_message_detail(
    access_token: str,
    message_id: str,
    include_thread: bool = True,
    thread_limit: int = 5,
) -> dict[str, Any]:
    """Return normalized Gmail message detail for action-item extraction."""
    detail_payload = _google_get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
        access_token=access_token,
        params={"format": "full"},
    )

    payload = detail_payload.get("payload", {})
    headers = payload.get("headers", []) if isinstance(payload, dict) else []
    if not isinstance(headers, list):
        headers = []

    plain_parts: list[str] = []
    html_parts: list[str] = []
    if isinstance(payload, dict):
        plain_parts, html_parts = _extract_gmail_body_parts(payload)

    body_text = "\n\n".join(part.strip() for part in plain_parts if part.strip()).strip()
    if not body_text:
        html_text = "\n\n".join(part.strip() for part in html_parts if part.strip()).strip()
        body_text = _strip_html(html_text)

    max_body_length = 20000
    body_truncated = len(body_text) > max_body_length
    normalized_body = body_text[:max_body_length]

    thread_context: list[dict[str, Any]] = []
    thread_id = str(detail_payload.get("threadId", "")).strip()
    if include_thread and thread_id:
        thread_payload = _google_get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
            access_token=access_token,
            params={
                "format": "metadata",
                "metadataHeaders": ["Subject", "From", "Date"],
            },
        )
        raw_thread_messages = thread_payload.get("messages")
        if isinstance(raw_thread_messages, list):
            safe_thread_limit = max(1, min(thread_limit, 10))
            for thread_message in raw_thread_messages[-safe_thread_limit:]:
                if not isinstance(thread_message, dict):
                    continue
                normalized = _normalize_thread_message(thread_message)
                if normalized["id"]:
                    thread_context.append(normalized)

    label_ids = detail_payload.get("labelIds", [])
    if not isinstance(label_ids, list):
        label_ids = []

    return {
        "id": str(detail_payload.get("id", "")).strip(),
        "thread_id": thread_id,
        "snippet": str(detail_payload.get("snippet", "")).strip(),
        "subject": _header_value(headers, "Subject"),
        "from": _header_value(headers, "From"),
        "to": _header_value(headers, "To"),
        "date": _header_value(headers, "Date"),
        "internal_date": str(detail_payload.get("internalDate", "")).strip(),
        "label_ids": [str(label).strip() for label in label_ids if str(label).strip()],
        "body_text": normalized_body,
        "body_truncated": body_truncated,
        "thread_context": thread_context,
    }


def get_calendar_freebusy(
    access_token: str,
    time_min: str | None = None,
    time_max: str | None = None,
) -> dict[str, Any]:
    """Return busy windows for the primary Google calendar."""
    now_utc = datetime.now(timezone.utc)
    start = time_min or now_utc.isoformat().replace("+00:00", "Z")
    end = time_max or (now_utc + timedelta(hours=24)).isoformat().replace("+00:00", "Z")

    payload = _google_post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
        access_token=access_token,
        payload={
            "timeMin": start,
            "timeMax": end,
            "items": [{"id": "primary"}],
        },
    )

    calendars = payload.get("calendars", {})
    primary = calendars.get("primary", {}) if isinstance(calendars, dict) else {}
    busy_windows = primary.get("busy", []) if isinstance(primary, dict) else []
    if not isinstance(busy_windows, list):
        busy_windows = []

    normalized_busy: list[dict[str, str]] = []
    for window in busy_windows:
        if not isinstance(window, dict):
            continue
        start_value = str(window.get("start", "")).strip()
        end_value = str(window.get("end", "")).strip()
        if not start_value or not end_value:
            continue
        normalized_busy.append({"start": start_value, "end": end_value})

    return {
        "time_min": start,
        "time_max": end,
        "time_zone": str(payload.get("timeZone", "")).strip(),
        "busy": normalized_busy,
    }
