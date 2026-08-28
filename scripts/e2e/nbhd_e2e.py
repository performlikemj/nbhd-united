#!/usr/bin/env python3
"""Production-safe command-line entry point for the dedicated NBHD E2E tenant."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

import fixtures

try:
    import smoke
    from client import (
        LEGACY_BASE_URL,
        POLL_DEADLINE_SECONDS,
        PRIMARY_BASE_URL,
        READ_DEADLINE_SECONDS,
        HarnessError,
        MessageObservation,
        NBHDClient,
        validate_cursor,
        validate_harness_message_id,
    )
except ModuleNotFoundError as exc:
    if exc.name != "requests":
        raise
    print("error: requests is unavailable; use the repo venv python: .venv/bin/python", file=sys.stderr)
    raise SystemExit(1) from None

_SAFE_LITERAL_STRINGS = frozenset(
    {
        "UNAVAILABLE",
        "app",
        "authenticated",
        "awake",
        "complete",
        "control_plane",
        "cron",
        "error",
        "line",
        "manual",
        "none",
        "on_device",
        "other",
        "passed",
        "pending",
        "ready",
        "redacted",
        "telegram",
        "tenant",
        "tenant-log-digest-not-deployed",
        "PERSON",
        "PHONE_NUMBER",
    }
)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "error: invalid arguments\n")


def _emit_safe(payload: dict[str, Any]) -> None:
    _validate_safe_value(payload)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _validate_safe_value(value: Any) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return
    if isinstance(value, str):
        if value in _SAFE_LITERAL_STRINGS:
            return
        try:
            validate_harness_message_id(value)
            return
        except HarnessError:
            pass
        from client import normalize_timestamp

        try:
            normalize_timestamp(value, nullable=False)
            return
        except HarnessError as exc:
            raise HarnessError("refusing unsafe output string") from exc
    if isinstance(value, list):
        for item in value:
            _validate_safe_value(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_safe_value(item)
        return
    raise HarnessError("refusing unsafe output shape")


def _client(args: argparse.Namespace) -> NBHDClient:
    return NBHDClient(base_url=LEGACY_BASE_URL if args.legacy else PRIMARY_BASE_URL)


def _authenticate(args: argparse.Namespace, *, deadline_seconds: float) -> tuple[NBHDClient, float]:
    client = _client(args)
    deadline = client.new_deadline(deadline_seconds)
    client.authenticate(deadline=deadline)
    return client, deadline


def _cleanup_warning() -> None:
    print("warning: disposable thread cleanup failed", file=sys.stderr)


def _redaction_types(observation: MessageObservation) -> list[str]:
    allowed = {"PERSON", "PHONE_NUMBER"}
    types = set()
    for row in observation.user_redactions:
        value = row["placeholder"][1:].rsplit("_", 1)[0]
        types.add(value if value in allowed else "other")
    return sorted(types)


def _cmd_login(args: argparse.Namespace) -> None:
    client = _client(args)
    deadline = client.new_deadline(READ_DEADLINE_SECONDS)
    password = getpass.getpass("NBHD password: ")
    client.login(password, deadline=deadline)
    _emit_safe({"status": "authenticated", "tenant_gate": "passed"})


def _cmd_wake(args: argparse.Namespace) -> None:
    client, deadline = _authenticate(args, deadline_seconds=POLL_DEADLINE_SECONDS)
    if not client.tenant_hibernated:
        _emit_safe({"status": "awake", "probe_sent": False})
        return
    with client.managed_thread(
        fixtures.thread_title(), deadline=deadline, cleanup_reporter=_cleanup_warning
    ) as thread_id:
        client_msg_id = fixtures.client_message_id("wake")
        client.send_message(
            text=fixtures.wake_message(),
            client_msg_id=client_msg_id,
            thread_id=thread_id,
            deadline=deadline,
        )
        observation = client.wait_for_message(client_msg_id, deadline=deadline)
        smoke.assert_ready(observation)
        _emit_safe({"status": "awake", "probe_sent": True, **observation.metadata()})


def _cmd_chat_send(args: argparse.Namespace) -> None:
    client, deadline = _authenticate(args, deadline_seconds=POLL_DEADLINE_SECONDS)
    with client.managed_thread(
        fixtures.thread_title(), deadline=deadline, cleanup_reporter=_cleanup_warning
    ) as thread_id:
        client_msg_id = fixtures.client_message_id("chat")
        observation = client.send_message(
            text=fixtures.smoke_message(),
            client_msg_id=client_msg_id,
            thread_id=thread_id,
            deadline=deadline,
        )
        if args.wait:
            observation = client.wait_for_message(client_msg_id, deadline=deadline)
            smoke.assert_ready(observation)
        _emit_safe(observation.metadata())


def _cmd_chat_history(args: argparse.Namespace) -> None:
    client, deadline = _authenticate(args, deadline_seconds=READ_DEADLINE_SECONDS)
    _emit_safe(client.history(since=args.since, limit=args.limit, deadline=deadline))


def _cmd_receipts(args: argparse.Namespace) -> None:
    client, deadline = _authenticate(args, deadline_seconds=READ_DEADLINE_SECONDS)
    observation = client.message_detail(args.message_id, deadline=deadline)
    payload = observation.metadata()
    payload["user_redaction_types"] = _redaction_types(observation)
    _emit_safe(payload)


def _cmd_pii_list(args: argparse.Namespace) -> None:
    client, deadline = _authenticate(args, deadline_seconds=READ_DEADLINE_SECONDS)
    entries = client.entity_registry(deadline=deadline)
    _emit_safe(
        {
            "count": len(entries),
            "fixture_person_present": any(entry.name == fixtures.PERSON_NAME for entry in entries),
            "fixture_phone_present": any(entry.name == fixtures.PHONE_NUMBER for entry in entries),
        }
    )


def _cmd_pii_count(args: argparse.Namespace) -> None:
    client, deadline = _authenticate(args, deadline_seconds=READ_DEADLINE_SECONDS)
    print(len(client.entity_registry(deadline=deadline)))


def _cmd_pii_keep(args: argparse.Namespace) -> None:
    client, deadline = _authenticate(args, deadline_seconds=READ_DEADLINE_SECONDS)
    _emit_safe(client.keep_fixture_pii(deadline=deadline))


def _cmd_pii_stop(args: argparse.Namespace) -> None:
    client, deadline = _authenticate(args, deadline_seconds=READ_DEADLINE_SECONDS)
    result = client.stop_pii(args.name, deadline=deadline)
    _emit_safe(
        {
            "status": "complete",
            "reason": result.reason,
            "decided_at": result.decided_at,
            "retired_count": result.retired_count,
        }
    )


def _cmd_smoke(args: argparse.Namespace) -> None:
    client = _client(args)
    deadline = client.new_deadline(POLL_DEADLINE_SECONDS)
    smoke.run_smoke(client, deadline=deadline, follow_up=args.follow_up)


def _cmd_logs(_args: argparse.Namespace) -> None:
    _emit_safe({"status": "UNAVAILABLE", "reason": "tenant-log-digest-not-deployed"})


def _history_limit(value: str) -> int:
    if not value.isascii() or not value.isdigit() or len(value) > 3:
        raise argparse.ArgumentTypeError("invalid history limit")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid history limit") from exc
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("invalid history limit")
    return parsed


def _history_cursor(value: str) -> str:
    try:
        return validate_cursor(value)
    except HarnessError as exc:
        raise argparse.ArgumentTypeError("invalid history cursor") from exc


def _receipt_id(value: str) -> str:
    try:
        return validate_harness_message_id(value)
    except HarnessError as exc:
        raise argparse.ArgumentTypeError("invalid harness message id") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description="NBHD allowlisted production E2E harness")
    parser.add_argument("--legacy", action="store_true", help="use the fixed legacy production host")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeArgumentParser)

    login = commands.add_parser("login")
    login.set_defaults(handler=_cmd_login)

    wake = commands.add_parser("wake")
    wake.set_defaults(handler=_cmd_wake)

    chat = commands.add_parser("chat")
    chat_commands = chat.add_subparsers(dest="chat_command", required=True, parser_class=SafeArgumentParser)
    chat_send = chat_commands.add_parser("send")
    chat_send.add_argument("--wait", action="store_true", required=True)
    chat_send.set_defaults(handler=_cmd_chat_send)
    history = chat_commands.add_parser("history")
    history.add_argument("--since", type=_history_cursor)
    history.add_argument("--limit", type=_history_limit, default=50)
    history.set_defaults(handler=_cmd_chat_history)

    receipts = commands.add_parser("receipts")
    receipts.add_argument("message_id", type=_receipt_id)
    receipts.set_defaults(handler=_cmd_receipts)

    pii = commands.add_parser("pii")
    pii_commands = pii.add_subparsers(dest="pii_command", required=True, parser_class=SafeArgumentParser)
    pii_list = pii_commands.add_parser("list")
    pii_list.set_defaults(handler=_cmd_pii_list)
    pii_count = pii_commands.add_parser("count")
    pii_count.set_defaults(handler=_cmd_pii_count)
    pii_keep = pii_commands.add_parser("keep")
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
