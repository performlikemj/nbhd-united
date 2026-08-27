#!/usr/bin/env python3
"""Capability boundary for the host skill's production command allowlist."""

from __future__ import annotations

import sys

import fixtures
import nbhd_e2e
from client import HarnessError, validate_cursor, validate_harness_message_id


def is_allowed_skill_command(argv: list[str]) -> bool:
    if argv in (["login"], ["wake"], ["chat", "send", "--wait"], ["pii", "list"], ["pii", "count"]):
        return True
    if argv in (["pii", "keep"], ["smoke"], ["smoke", "--follow-up"], ["logs"]):
        return True
    if len(argv) == 3 and argv[:2] == ["pii", "stop"]:
        return argv[2] in {fixtures.PERSON_NAME, fixtures.PHONE_NUMBER}
    if len(argv) == 2 and argv[0] == "receipts":
        try:
            validate_harness_message_id(argv[1])
        except HarnessError:
            return False
        return True
    if len(argv) >= 2 and argv[:2] == ["chat", "history"]:
        return _valid_history_args(argv[2:])
    return False


def _valid_history_args(argv: list[str]) -> bool:
    seen: set[str] = set()
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag not in {"--limit", "--since"} or flag in seen or index + 1 >= len(argv):
            return False
        value = argv[index + 1]
        if flag == "--limit":
            if not value.isascii() or not value.isdigit() or len(value) > 3:
                return False
            try:
                limit = int(value)
            except ValueError:
                return False
            if not 1 <= limit <= 100:
                return False
        else:
            try:
                validate_cursor(value)
            except HarnessError:
                return False
        seen.add(flag)
        index += 2
    return True


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not is_allowed_skill_command(arguments):
        print("error: command is not allowed by the nbhd-e2e skill", file=sys.stderr)
        return 2
    return nbhd_e2e.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
