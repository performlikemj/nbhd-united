"""Fixed, visibly synthetic inputs for the NBHD real-flow E2E harness."""

from __future__ import annotations

import uuid

MESSAGE_PREFIX = "[NBHD E2E SYNTHETIC]"
PERSON_NAME = "Evelyn Testwell"
PHONE_NUMBER = "+1 202-555-0147"


def _fixture_message(body: str) -> str:
    """Build a message from checked-in fixture text, never caller content."""
    return f"{MESSAGE_PREFIX} {body}"


def smoke_message() -> str:
    return _fixture_message(
        f"My test contact is {PERSON_NAME}. Her number is {PHONE_NUMBER}. Reply with a brief acknowledgement."
    )


def wake_message() -> str:
    return _fixture_message("Wake probe. Reply with a brief acknowledgement.")


def follow_up_message() -> str:
    return _fixture_message(f"Follow-up about {PERSON_NAME}. Reply with a brief acknowledgement.")


def thread_title() -> str:
    return f"{MESSAGE_PREFIX} disposable {uuid.uuid4().hex[:12]}"


def client_message_id(kind: str) -> str:
    if kind not in {"chat", "follow-up", "smoke", "wake"}:
        raise ValueError("unknown fixture message kind")
    return f"nbhd-e2e-{kind}-{uuid.uuid4().hex}"
