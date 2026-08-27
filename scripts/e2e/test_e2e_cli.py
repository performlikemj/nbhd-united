"""Pure unit tests for the real-flow harness. No test can reach a real host."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import fixtures
import keychain
import nbhd_e2e
import smoke
from client import HarnessError, NBHDClient, TenantGateError, message_metadata, observe_message, poll_interval


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


@contextmanager
def allowed_tenant_file(tenant_id: str):
    with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as handle:
        json.dump({"tenant_id": tenant_id}, handle)
        handle.flush()
        yield Path(handle.name)


def message_shape(*, receipt: bool = True) -> dict:
    payload = {
        "client_msg_id": "nbhd-e2e-smoke-abc",
        "status": "ready",
        "source": "tenant",
        "error": "",
        "reply_text": "private assistant reply",
        "created_at": "2026-08-28T00:00:00Z",
        "replied_at": "2026-08-28T00:00:01Z",
        "waking_at": None,
        "phase": "done",
        "user_redactions": [
            {"placeholder": "[PERSON_1]", "value": fixtures.PERSON_NAME},
            {"placeholder": "[PHONE_NUMBER_1]", "value": fixtures.PHONE_NUMBER},
        ],
        "reply_redactions": [{"placeholder": "[PERSON_1]", "value": fixtures.PERSON_NAME}],
    }
    if receipt:
        payload.update({"redaction_confirmed": True, "redaction_reason": "redacted"})
    return payload


class TenantGateTests(unittest.TestCase):
    @mock.patch("client.keychain.read_tokens", return_value=("access-token", "refresh-token"))
    def test_allowlist_mismatch_fails_closed(self, _read_tokens):
        with allowed_tenant_file("allowed-tenant") as allowed:
            session = FakeSession([FakeResponse(200, {"tenant": {"id": "different-tenant"}})])
            client = NBHDClient(session=session, allowed_tenants_path=allowed)
            with self.assertRaises(TenantGateError):
                client.authenticate()
            self.assertIsNone(client.access_token)
            self.assertIsNone(client.refresh_token)

    def test_placeholder_allowlist_fails_before_http(self):
        with allowed_tenant_file("REPLACE-AFTER-PROVISION") as allowed:
            session = FakeSession([])
            client = NBHDClient(session=session, allowed_tenants_path=allowed)
            with self.assertRaises(TenantGateError):
                client._tenant_gate("access-token")
            self.assertEqual(session.calls, [])

    def test_arbitrary_base_url_is_refused(self):
        with self.assertRaises(HarnessError):
            NBHDClient(base_url="https://example.invalid")

    def test_retry_after_is_honored_once(self):
        delays = []
        session = FakeSession(
            [
                FakeResponse(429, headers={"Retry-After": "2.5"}),
                FakeResponse(200, {"messages": [], "cursor": None}),
            ]
        )
        client = NBHDClient(session=session, sleep=delays.append)
        client.access_token = "access-token"
        result = client.history()
        self.assertEqual(result["count"], 0)
        self.assertEqual(delays, [2.5])
        self.assertEqual(len(session.calls), 2)

    @mock.patch("client.keychain.write_secret")
    @mock.patch("client.keychain.read_tokens", return_value=("old-access", "refresh-token"))
    def test_401_refreshes_gates_and_retries_once(self, _read_tokens, write_secret):
        tenant_id = "allowed-tenant"
        responses = [
            FakeResponse(200, {"tenant": {"id": tenant_id}}),
            FakeResponse(401),
            FakeResponse(200, {"access": "new-access"}),
            FakeResponse(200, {"tenant": {"id": tenant_id}}),
            FakeResponse(200, {"messages": [], "cursor": None}),
        ]
        with allowed_tenant_file(tenant_id) as allowed:
            session = FakeSession(responses)
            client = NBHDClient(session=session, allowed_tenants_path=allowed)
            client.authenticate()
            result = client.history()
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(session.calls), 5)
        self.assertEqual(session.calls[-1]["headers"]["Authorization"], "Bearer new-access")
        write_secret.assert_called_once_with(keychain.ACCESS_ACCOUNT, "new-access")

    @mock.patch("client.keychain.write_secret")
    @mock.patch("client.keychain.read_tokens", return_value=("expired-access", "refresh-token"))
    def test_expired_stored_access_refreshes_before_gate(self, _read_tokens, write_secret):
        tenant_id = "allowed-tenant"
        responses = [
            FakeResponse(401),
            FakeResponse(200, {"access": "new-access"}),
            FakeResponse(200, {"tenant": {"id": tenant_id}}),
        ]
        with allowed_tenant_file(tenant_id) as allowed:
            session = FakeSession(responses)
            client = NBHDClient(session=session, allowed_tenants_path=allowed)
            profile = client.authenticate()
        self.assertEqual(profile["tenant"]["id"], tenant_id)
        self.assertEqual(len(session.calls), 3)
        write_secret.assert_called_once_with(keychain.ACCESS_ACCOUNT, "new-access")


class ScheduleAndReceiptTests(unittest.TestCase):
    def test_poll_schedule_boundaries(self):
        self.assertEqual(poll_interval(0), 1.5)
        self.assertEqual(poll_interval(179.999), 1.5)
        self.assertEqual(poll_interval(180), 5.0)
        self.assertEqual(poll_interval(299.999), 5.0)
        self.assertEqual(poll_interval(300), 15.0)
        self.assertEqual(poll_interval(899.999), 15.0)

    def test_receipt_absence_is_feature_detected_and_skipped(self):
        observation = observe_message(message_shape(receipt=False), expected_client_msg_id="nbhd-e2e-smoke-abc")
        events = []
        self.assertFalse(smoke.assert_durable_receipt(observation, lambda *parts: events.append(parts)))
        self.assertEqual(events, [(5, "durable-redaction-receipt", "SKIPPED")])
        smoke.assert_fixture_redactions(observation)

    def test_present_receipt_remains_strict(self):
        payload = message_shape()
        payload["redaction_confirmed"] = False
        observation = observe_message(payload, expected_client_msg_id="nbhd-e2e-smoke-abc")
        with self.assertRaises(HarnessError):
            smoke.assert_durable_receipt(observation, lambda *_args: None)


class PrivacyTests(unittest.TestCase):
    def test_metadata_output_discards_reply_and_mapping_values(self):
        payload = message_shape()
        observation = observe_message(payload, expected_client_msg_id="nbhd-e2e-smoke-abc")
        output = io.StringIO()
        with redirect_stdout(output):
            nbhd_e2e._emit(observation.metadata())
        rendered = output.getvalue()
        self.assertNotIn(payload["reply_text"], rendered)
        self.assertNotIn(fixtures.PERSON_NAME, rendered)
        self.assertNotIn(fixtures.PHONE_NUMBER, rendered)
        self.assertNotIn("value", rendered)

    def test_history_metadata_discards_content_fields(self):
        safe = message_metadata(message_shape())
        self.assertNotIn("reply_text", safe)
        self.assertNotIn("user_text", safe)
        self.assertNotIn("user_redactions", safe)
        self.assertEqual(safe["user_redaction_count"], 2)

    def test_chat_cli_has_no_free_text_argument(self):
        args = nbhd_e2e.build_parser().parse_args(["chat", "send", "--wait"])
        self.assertEqual(args.handler, nbhd_e2e._cmd_chat_send)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            nbhd_e2e.build_parser().parse_args(["chat", "send", "--wait", "arbitrary text"])
        fixture_messages = [fixtures.smoke_message(), fixtures.wake_message(), fixtures.follow_up_message()]
        self.assertTrue(all(message.startswith(fixtures.MESSAGE_PREFIX) for message in fixture_messages))

    @mock.patch("keychain.subprocess.run")
    def test_keychain_write_uses_stdin_not_argv(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        keychain.write_secret(keychain.ACCESS_ACCOUNT, "memory-only-token")
        argv = run.call_args.args[0]
        self.assertNotIn("memory-only-token", argv)
        self.assertEqual(argv[-1], "-w")
        self.assertEqual(run.call_args.kwargs["input"], "memory-only-token\n")
        self.assertTrue(run.call_args.kwargs["capture_output"])

    @mock.patch("keychain.subprocess.run")
    def test_keychain_read_captures_secret_in_memory(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "memory-only-token\n", "")
        self.assertEqual(keychain.read_secret(keychain.REFRESH_ACCOUNT), "memory-only-token")
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertEqual(run.call_args.args[0][-1], "-w")


if __name__ == "__main__":
    unittest.main()
