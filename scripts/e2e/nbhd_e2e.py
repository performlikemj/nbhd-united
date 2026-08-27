#!/usr/bin/env python3
"""Safe command-line entry point for NBHD's dedicated real-flow E2E tenant."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from typing import Any

import fixtures
import smoke
from client import (
    DEVELOPMENT_BASE_URL,
    LEGACY_BASE_URL,
    PRIMARY_BASE_URL,
    HarnessError,
    MessageObservation,
    NBHDClient,
)

_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_PLACEHOLDER_RE = re.compile(r"^\[[A-Z][A-Z_]*_[1-9][0-9]*\]$")


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _client(args: argparse.Namespace) -> NBHDClient:
    if args.development:
        base_url = DEVELOPMENT_BASE_URL
    elif args.legacy:
        base_url = LEGACY_BASE_URL
    else:
        base_url = PRIMARY_BASE_URL
    return NBHDClient(base_url=base_url, development=args.development)


def _authenticate(args: argparse.Namespace) -> NBHDClient:
    client = _client(args)
    client.authenticate()
    return client


def _redaction_types(observation: MessageObservation) -> list[str]:
    types = set()
    for row in observation.user_redactions:
        placeholder = row.get("placeholder")
        if isinstance(placeholder, str) and placeholder.startswith("[") and "_" in placeholder:
            types.add(placeholder[1:].rsplit("_", 1)[0])
    return sorted(types)


def _cmd_login(args: argparse.Namespace) -> None:
    password = getpass.getpass("NBHD password: ")
    _client(args).login(args.email, password)
    _emit({"status": "authenticated", "tenant_gate": "passed"})


def _cmd_wake(args: argparse.Namespace) -> None:
    client = _authenticate(args)
    tenant = (client.tenant_profile or {}).get("tenant")
    if not isinstance(tenant, dict):
        raise HarnessError("tenant profile missing")
    if not tenant.get("hibernated_at"):
        _emit({"status": "awake", "probe_sent": False})
        return
    thread_id = client.create_thread(fixtures.thread_title())
    try:
        client_msg_id = fixtures.client_message_id("wake")
        client.send_message(text=fixtures.wake_message(), client_msg_id=client_msg_id, thread_id=thread_id)
        observation = client.wait_for_message(client_msg_id)
        smoke.assert_ready(observation)
        _emit({"status": "awake", "probe_sent": True, **observation.metadata()})
    finally:
        client.delete_thread(thread_id)


def _cmd_chat_send(args: argparse.Namespace) -> None:
    client = _authenticate(args)
    thread_id = client.create_thread(fixtures.thread_title())
    try:
        client_msg_id = fixtures.client_message_id("chat")
        observation = client.send_message(
            text=fixtures.smoke_message(), client_msg_id=client_msg_id, thread_id=thread_id
        )
        if args.wait:
            observation = client.wait_for_message(client_msg_id)
            smoke.assert_ready(observation)
        _emit(observation.metadata())
    finally:
        client.delete_thread(thread_id)


def _cmd_chat_history(args: argparse.Namespace) -> None:
    _emit(_authenticate(args).history(since=args.since, limit=args.limit))


def _cmd_receipts(args: argparse.Namespace) -> None:
    if not _MESSAGE_ID_RE.fullmatch(args.message_id):
        raise HarnessError("invalid message id")
    observation = _authenticate(args).message_detail(args.message_id)
    payload = observation.metadata()
    payload["user_redaction_types"] = _redaction_types(observation)
    _emit(payload)


def _cmd_pii_list(args: argparse.Namespace) -> None:
    entries = _authenticate(args).entity_registry()
    safe_entries = []
    for entry in entries:
        placeholder = entry.get("placeholder")
        safe_entries.append(
            {
                "placeholder": placeholder,
                "entity_type": (
                    placeholder[1:].rsplit("_", 1)[0]
                    if isinstance(placeholder, str) and placeholder.startswith("[") and "_" in placeholder
                    else None
                ),
                "updated_at": entry.get("updated_at"),
            }
        )
    _emit({"count": len(entries), "entries": safe_entries})


def _cmd_pii_count(args: argparse.Namespace) -> None:
    print(len(_authenticate(args).entity_registry()))


def _cmd_pii_keep(args: argparse.Namespace) -> None:
    if not all(_PLACEHOLDER_RE.fullmatch(value) for value in args.placeholders):
        raise HarnessError("invalid PII placeholder")
    payload = _authenticate(args).keep_pii(args.placeholders)
    kept = payload.get("kept")
    not_found = payload.get("not_found")
    _emit(
        {
            "status": "complete",
            "kept_count": len(kept) if isinstance(kept, list) else 0,
            "not_found_count": len(not_found) if isinstance(not_found, list) else 0,
        }
    )


def _cmd_pii_stop(args: argparse.Namespace) -> None:
    if args.name not in {fixtures.PERSON_NAME, fixtures.PHONE_NUMBER}:
        raise HarnessError("pii stop accepts fixed fixture values only")
    payload = _authenticate(args).stop_pii(args.name)
    _emit(
        {
            "status": "complete",
            "key_present": isinstance(payload.get("key"), str),
            "reason": payload.get("reason"),
            "decided_at": payload.get("decided_at"),
            "retired_count": payload.get("retired"),
        }
    )


def _cmd_smoke(args: argparse.Namespace) -> None:
    smoke.run_smoke(_client(args), follow_up=args.follow_up)


def _cmd_logs(_args: argparse.Namespace) -> None:
    _emit({"status": "UNAVAILABLE", "reason": "tenant-log-digest lane not deployed"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NBHD allowlisted real-flow E2E harness")
    host_group = parser.add_mutually_exclusive_group()
    host_group.add_argument("--legacy", action="store_true", help="use the fixed legacy production host")
    host_group.add_argument("--development", action="store_true", help="explicitly use fixed http://localhost:8000")
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login")
    login.add_argument("--email", required=True)
    login.set_defaults(handler=_cmd_login)

    wake = commands.add_parser("wake")
    wake.set_defaults(handler=_cmd_wake)

    chat = commands.add_parser("chat")
    chat_commands = chat.add_subparsers(dest="chat_command", required=True)
    chat_send = chat_commands.add_parser("send")
    chat_send.add_argument("--wait", action="store_true", required=True)
    chat_send.set_defaults(handler=_cmd_chat_send)
    history = chat_commands.add_parser("history")
    history.add_argument("--since")
    history.add_argument("--limit", type=int, default=50)
    history.set_defaults(handler=_cmd_chat_history)

    receipts = commands.add_parser("receipts")
    receipts.add_argument("message_id")
    receipts.set_defaults(handler=_cmd_receipts)

    pii = commands.add_parser("pii")
    pii_commands = pii.add_subparsers(dest="pii_command", required=True)
    pii_list = pii_commands.add_parser("list")
    pii_list.set_defaults(handler=_cmd_pii_list)
    pii_count = pii_commands.add_parser("count")
    pii_count.set_defaults(handler=_cmd_pii_count)
    pii_keep = pii_commands.add_parser("keep")
    pii_keep.add_argument("placeholders", nargs="+")
    pii_keep.set_defaults(handler=_cmd_pii_keep)
    pii_stop = pii_commands.add_parser("stop")
    pii_stop.add_argument("name", choices=(fixtures.PERSON_NAME, fixtures.PHONE_NUMBER))
    pii_stop.set_defaults(handler=_cmd_pii_stop)

    smoke_parser = commands.add_parser("smoke")
    smoke_parser.add_argument("--follow-up", action="store_true")
    smoke_parser.set_defaults(handler=_cmd_smoke)

    logs = commands.add_parser("logs")
    logs.set_defaults(handler=_cmd_logs)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
