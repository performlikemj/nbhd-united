#!/usr/bin/env python3
"""Request user approval for a destructive action via the NBHD gate API.

Called by the OpenClaw agent as a tool. Sends a confirmation request to
Django, then polls for the user's response.

Environment:
    NBHD_API_BASE_URL  — Django backend URL (e.g. https://api.neighborhoodunited.org)
    NBHD_INTERNAL_API_KEY — shared internal auth key
    NBHD_TENANT_ID — tenant UUID

Arguments (via stdin JSON or positional):
    action_type     — gmail_trash, gmail_delete, gmail_send, calendar_delete,
                      drive_delete, task_delete
    display_summary — human-readable description
    payload         — (optional) JSON object with action-specific IDs
"""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_BASE = os.environ.get("NBHD_API_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("NBHD_INTERNAL_API_KEY", "")
TENANT_ID = os.environ.get("NBHD_TENANT_ID", "")

POLL_INTERVAL = 3  # seconds
MAX_POLL_TIME = 310  # just over 5 minutes


def _api_call(method: str, path: str, body: dict | None = None) -> dict:
    """Make an authenticated API call to Django."""
    url = f"{API_BASE}{path}"
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Key": API_KEY,
        "X-Tenant-Id": TENANT_ID,
    }
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)

    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body_text)
        except json.JSONDecodeError:
            return {"error": body_text, "status_code": e.code}
    except URLError as e:
        return {"error": str(e)}


def main():
    if not all([API_BASE, API_KEY, TENANT_ID]):
        print(json.dumps({
            "status": "error",
            "message": "Missing required environment variables (NBHD_API_BASE_URL, NBHD_INTERNAL_API_KEY, NBHD_TENANT_ID)",
        }))
        return

    # Parse arguments from stdin (OpenClaw passes tool args as JSON)
    try:
        raw = sys.stdin.read().strip()
        if raw:
            args = json.loads(raw)
        else:
            args = {}
    except json.JSONDecodeError:
        args = {}

    action_type = args.get("action_type", "")
    display_summary = args.get("display_summary", "")
    payload = args.get("payload", {})

    if not action_type or not display_summary:
        print(json.dumps({
            "status": "error",
            "message": "action_type and display_summary are required",
        }))
        return

    # Step 1: Create the gate request
    result = _api_call("POST", f"/api/v1/internal/runtime/{TENANT_ID}/gate/request/", {
        "action_type": action_type,
        "payload": payload,
        "display_summary": display_summary,
    })

    status = result.get("status", "")

    # Immediate resolution (blocked, auto-approved, or error)
    if status == "blocked":
        print(json.dumps({
            "status": "blocked",
            "tier": result.get("tier", "starter"),
            "message": result.get("message", "This action is not available on your current plan."),
        }))
        return

    if status == "approved" and result.get("auto_approved"):
        print(json.dumps({
            "status": "approved",
            "auto_approved": True,
            "message": "Action auto-approved (user has disabled confirmation for this action type).",
        }))
        return

    if status == "error" or "error" in result:
        print(json.dumps({
            "status": "error",
            "message": result.get("error", result.get("message", "Unknown error")),
        }))
        return

    if status != "pending":
        print(json.dumps({
            "status": "error",
            "message": f"Unexpected status: {status}",
        }))
        return

    # Step 2: Poll for user's response
    action_id = result["action_id"]
    elapsed = 0

    while elapsed < MAX_POLL_TIME:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        poll_result = _api_call(
            "GET",
            f"/api/v1/internal/runtime/{TENANT_ID}/gate/{action_id}/poll/",
        )

        poll_status = poll_result.get("status", "")

        if poll_status == "approved":
            print(json.dumps({
                "status": "approved",
                "action_id": action_id,
                "message": "User approved this action. You may proceed.",
            }))
            return

        if poll_status == "denied":
            print(json.dumps({
                "status": "denied",
                "action_id": action_id,
                "message": "User denied this action. Do not proceed.",
            }))
            return

        if poll_status == "expired":
            print(json.dumps({
                "status": "expired",
                "action_id": action_id,
                "message": "Confirmation timed out (5 minutes). The user did not respond.",
            }))
            return

        # Still pending — keep polling

    # Timed out on our side
    print(json.dumps({
        "status": "expired",
        "action_id": action_id,
        "message": "Confirmation timed out.",
    }))


if __name__ == "__main__":
    main()
