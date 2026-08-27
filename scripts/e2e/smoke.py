"""Nine-step real-flow smoke using only the fixed synthetic fixtures."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import fixtures
from client import HarnessError, MessageObservation, NBHDClient

StepLogger = Callable[[int, str, str], None]


def _redaction_type(placeholder: Any) -> str | None:
    if not isinstance(placeholder, str) or not placeholder.startswith("[") or "_" not in placeholder:
        return None
    return placeholder[1:].rsplit("_", 1)[0]


def assert_ready(observation: MessageObservation) -> None:
    if observation.status != "ready":
        raise HarnessError("smoke reply did not reach ready")
    if observation.source != "tenant":
        raise HarnessError("smoke reply source was not tenant")
    if observation.error:
        raise HarnessError("smoke reply contained an error")
    if not observation.reply_nonempty:
        raise HarnessError("smoke reply was empty")


def assert_durable_receipt(observation: MessageObservation, log: StepLogger) -> bool:
    """Feature-detect PR-A's additive receipt while keeping all other checks strict."""
    if not observation.receipt_present:
        log(5, "durable-redaction-receipt", "SKIPPED")
        return False
    if observation.redaction_confirmed is not True or observation.redaction_reason != "redacted":
        raise HarnessError("durable redaction receipt was not confirmed/redacted")
    return True


def assert_fixture_redactions(observation: MessageObservation) -> None:
    pairs = {(_redaction_type(row.get("placeholder")), row.get("value")) for row in observation.user_redactions}
    expected = {("PERSON", fixtures.PERSON_NAME), ("PHONE_NUMBER", fixtures.PHONE_NUMBER)}
    if not expected.issubset(pairs):
        raise HarnessError("smoke did not create both fixture redactions")


def assert_synthetic_tenant(profile: dict[str, Any]) -> None:
    tenant = profile.get("tenant")
    if not isinstance(tenant, dict):
        raise HarnessError("tenant profile missing")
    if tenant.get("is_synthetic") is not True:
        raise HarnessError("tenant profile is not explicitly synthetic")
    if tenant.get("is_eval_sink") is not False:
        raise HarnessError("tenant profile is not explicitly non-eval-sink")


def _registry_contains_fixtures(entries: list[dict[str, Any]]) -> bool:
    names = {row.get("name") for row in entries}
    return fixtures.PERSON_NAME in names and fixtures.PHONE_NUMBER in names


def _default_log(step: int, assertion: str, result: str) -> None:
    print(f"step={step} assertion={assertion} status={result}")


def run_smoke(
    client: NBHDClient,
    *,
    follow_up: bool = False,
    log: StepLogger = _default_log,
) -> None:
    thread_id: str | None = None
    try:
        profile = client.authenticate()
        assert_synthetic_tenant(profile)
        log(1, "allowlisted-synthetic-non-eval-sink", "PASS")

        thread_id = client.create_thread(fixtures.thread_title())
        log(2, "disposable-non-main-thread", "PASS")

        client_msg_id = fixtures.client_message_id("smoke")
        client.send_message(
            text=fixtures.smoke_message(),
            client_msg_id=client_msg_id,
            thread_id=thread_id,
        )
        log(3, "fixed-synthetic-turn-sent", "PASS")

        observation = client.wait_for_message(client_msg_id)
        assert_ready(observation)
        log(4, "tenant-reply-ready-content-present", "PASS")

        receipt_checked = assert_durable_receipt(observation, log)
        assert_fixture_redactions(observation)
        if receipt_checked:
            log(5, "durable-redaction-receipt-and-two-mappings", "PASS")
        else:
            log(5, "two-redaction-mappings", "PASS")

        registry = client.entity_registry()
        if not _registry_contains_fixtures(registry):
            raise HarnessError("entity registry did not contain both fixtures")
        log(6, "entity-registry-contains-two-fixtures", "PASS")

        stopped = client.stop_pii(fixtures.PERSON_NAME)
        expected_key = fixtures.PERSON_NAME.casefold()
        if stopped.get("key") != expected_key or not isinstance(stopped.get("retired"), int):
            raise HarnessError("fixture denylist response was invalid")
        denylist = client.pii_denylist()
        if expected_key not in {entry.get("key") for entry in denylist}:
            raise HarnessError("fixture canonical key was not denylisted")
        log(7, "fixture-name-denylisted-and-binding-retired", "PASS")

        if follow_up:
            follow_up_id = fixtures.client_message_id("follow-up")
            client.send_message(
                text=fixtures.follow_up_message(),
                client_msg_id=follow_up_id,
                thread_id=thread_id,
            )
            follow_up_observation = client.wait_for_message(follow_up_id)
            assert_ready(follow_up_observation)
            if any(row.get("value") == fixtures.PERSON_NAME for row in follow_up_observation.user_redactions):
                raise HarnessError("denylisted fixture name was redacted on follow-up")
            log(8, "denylisted-name-not-redacted-on-follow-up", "PASS")
        else:
            log(8, "optional-follow-up", "SKIPPED")
    finally:
        if thread_id is not None:
            client.delete_thread(thread_id)
            log(9, "disposable-thread-deleted-pii-state-retained", "PASS")
