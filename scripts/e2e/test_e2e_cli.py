"""Pure security-contract tests for the real-flow harness. No real network calls."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import fixtures
import keychain
import nbhd_e2e
import nbhd_e2e_skill
import smoke
from client import (
    DeadlineExceeded,
    HarnessError,
    HTTPStatusError,
    NBHDClient,
    TenantGateError,
    load_allowlist,
    observe_message,
    poll_interval,
)

TENANT_ID = "123e4567-e89b-42d3-a456-426614174000"
OTHER_TENANT_ID = "223e4567-e89b-42d3-a456-426614174000"
THREAD_ID = "323e4567-e89b-42d3-a456-426614174000"
MESSAGE_ID = f"nbhd-e2e-smoke-{'a' * 32}"
EMAIL = "dedicated-e2e@example.invalid"


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


class FakeClock:
    def __init__(self, value: float = 100.0):
        self.value = value
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


@contextmanager
def allowlist_file(payload: dict | None = None):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "allowed-tenants.json"
        path.write_text(json.dumps(payload or {"tenant_id": TENANT_ID, "email": EMAIL}), encoding="utf-8")
        path.chmod(0o600)
        yield path


def tenant_profile(
    *,
    tenant_id: str = TENANT_ID,
    is_synthetic: bool = True,
    is_eval_sink: bool = False,
    include_flags: bool = True,
) -> dict:
    tenant = {"id": tenant_id, "hibernated_at": None}
    if include_flags:
        tenant.update({"is_synthetic": is_synthetic, "is_eval_sink": is_eval_sink})
    return tenant


def message_shape(*, receipt: bool = True) -> dict:
    payload = {
        "client_msg_id": MESSAGE_ID,
        "status": "ready",
        "source": "tenant",
        "error": "",
        "reply_text": "private assistant reply",
        "created_at": "2026-08-28T00:00:00Z",
        "replied_at": "2026-08-28T00:00:01Z",
        "retried_at": None,
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


class ProductionBoundaryTests(unittest.TestCase):
    def test_localhost_and_development_flag_do_not_exist(self):
        with self.assertRaises(HarnessError):
            NBHDClient(base_url="http://localhost:8000")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            nbhd_e2e.build_parser().parse_args(["--development", "logs"])
        self.assertFalse(nbhd_e2e_skill.is_allowed_skill_command(["--development", "logs"]))
        self.assertFalse(nbhd_e2e_skill.is_allowed_skill_command(["--legacy", "wake"]))

    def test_runtime_skill_wrapper_has_a_closed_command_surface(self):
        self.assertTrue(nbhd_e2e_skill.is_allowed_skill_command(["chat", "send", "--wait"]))
        self.assertTrue(nbhd_e2e_skill.is_allowed_skill_command(["receipts", MESSAGE_ID]))
        self.assertFalse(nbhd_e2e_skill.is_allowed_skill_command(["login", "arbitrary@example.invalid"]))
        self.assertFalse(nbhd_e2e_skill.is_allowed_skill_command(["chat", "send", "--wait", "free text"]))
        self.assertFalse(nbhd_e2e_skill.is_allowed_skill_command(["receipts", "arbitrary-id"]))

    def test_redirects_are_disabled_and_3xx_is_rejected(self):
        session = FakeSession([FakeResponse(307, headers={"Location": "https://example.invalid/steal"})])
        client = NBHDClient(session=session)
        client.access_token = "access-token"
        with self.assertRaises(HTTPStatusError) as raised:
            client.history(deadline=client.new_deadline(100))
        self.assertIs(session.calls[0]["allow_redirects"], False)
        self.assertNotIn("example.invalid", str(raised.exception))


class AllowlistTests(unittest.TestCase):
    def test_exact_schema_uuid4_and_email_are_required(self):
        with allowlist_file() as path:
            loaded = load_allowlist(path)
        self.assertEqual(loaded.tenant_id, TENANT_ID)
        self.assertEqual(loaded.email, EMAIL)

        invalid_payloads = [
            {"tenant_id": TENANT_ID},
            {"tenant_id": TENANT_ID, "email": EMAIL, "extra": True},
            {"tenant_id": "not-a-uuid", "email": EMAIL},
            {"tenant_id": "11111111-1111-1111-8111-111111111111", "email": EMAIL},
            {"tenant_id": TENANT_ID.upper(), "email": EMAIL},
            {"tenant_id": TENANT_ID, "email": "not-an-email"},
            {"tenant_id": "REPLACE-AFTER-PROVISION", "email": "REPLACE-AFTER-PROVISION"},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), allowlist_file(payload) as path, self.assertRaises(TenantGateError):
                load_allowlist(path)

    def test_symlink_allowlist_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.json"
            target.write_text(json.dumps({"tenant_id": TENANT_ID, "email": EMAIL}), encoding="utf-8")
            target.chmod(0o600)
            link = Path(directory) / "allowed-tenants.json"
            os.symlink(target, link)
            with self.assertRaises(TenantGateError):
                load_allowlist(link)

    def test_login_uses_only_allowlisted_email(self):
        responses = [
            FakeResponse(200, {"access": "access-token", "refresh": "refresh-token"}),
            FakeResponse(200, tenant_profile()),
        ]
        with allowlist_file() as path, mock.patch("client.keychain.write_credentials"):
            session = FakeSession(responses)
            client = NBHDClient(session=session, allowed_tenants_path=path)
            client.login("password", deadline=client.new_deadline(100))
        self.assertEqual(session.calls[0]["json"]["email"], EMAIL)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            nbhd_e2e.build_parser().parse_args(["login", "--email", "other@example.invalid"])


class TenantGateTests(unittest.TestCase):
    @mock.patch(
        "client.keychain.read_credentials",
        return_value=keychain.Credentials(access="access-token", refresh="refresh-token"),
    )
    def test_id_mismatch_fails_closed(self, _read):
        with allowlist_file() as path:
            client = NBHDClient(
                session=FakeSession([FakeResponse(200, tenant_profile(tenant_id=OTHER_TENANT_ID))]),
                allowed_tenants_path=path,
            )
            with self.assertRaises(TenantGateError):
                client.authenticate(deadline=client.new_deadline(100))
            self.assertIsNone(client.access_token)
            self.assertIsNone(client.refresh_token)

    @mock.patch(
        "client.keychain.read_credentials",
        return_value=keychain.Credentials(access="access-token", refresh="refresh-token"),
    )
    def test_missing_synthetic_contract_names_shipping_pr_and_fails_closed(self, _read):
        with allowlist_file() as path:
            client = NBHDClient(
                session=FakeSession([FakeResponse(200, tenant_profile(include_flags=False))]),
                allowed_tenants_path=path,
            )
            with self.assertRaisesRegex(TenantGateError, "feat/chat-redaction-receipt"):
                client.authenticate(deadline=client.new_deadline(100))
            self.assertIsNone(client.access_token)

    @mock.patch(
        "client.keychain.read_credentials",
        return_value=keychain.Credentials(access="access-token", refresh="refresh-token"),
    )
    def test_non_synthetic_or_eval_sink_fails_every_command_gate(self, _read):
        for profile in (
            tenant_profile(is_synthetic=False),
            tenant_profile(is_eval_sink=True),
        ):
            with self.subTest(profile=profile), allowlist_file() as path:
                client = NBHDClient(session=FakeSession([FakeResponse(200, profile)]), allowed_tenants_path=path)
                with self.assertRaises(TenantGateError):
                    client.authenticate(deadline=client.new_deadline(100))


class OutputStrictnessTests(unittest.TestCase):
    def test_untrusted_output_string_is_refused_without_printing(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(HarnessError):
            nbhd_e2e._emit_safe({"error": "server-provided secret text"})
        self.assertEqual(output.getvalue(), "")

    def test_server_free_strings_map_to_closed_values(self):
        payload = message_shape()
        payload.update(
            {
                "status": "future-status-with-content",
                "source": "future-source-with-content",
                "error": "raw server error content",
                "redaction_reason": "raw reason content",
            }
        )
        observation = observe_message(payload, expected_client_msg_id=MESSAGE_ID)
        self.assertEqual(observation.status, "other")
        self.assertEqual(observation.source, "other")
        self.assertEqual(observation.error, "other")
        self.assertEqual(observation.redaction_reason, "other")
        rendered = io.StringIO()
        with redirect_stdout(rendered):
            nbhd_e2e._emit_safe(observation.metadata())
        self.assertNotIn("raw", rendered.getvalue())
        self.assertNotIn("future", rendered.getvalue())

    def test_unexpected_server_shapes_fail_closed(self):
        payload = message_shape()
        payload["error"] = {"reply_text": "secret"}
        with self.assertRaises(HarnessError):
            observe_message(payload, expected_client_msg_id=MESSAGE_ID)
        payload = message_shape()
        payload["created_at"] = "not-a-timestamp"
        with self.assertRaises(HarnessError):
            observe_message(payload, expected_client_msg_id=MESSAGE_ID)

    def test_receipt_id_and_history_cursor_are_bounded(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            nbhd_e2e.build_parser().parse_args(["receipts", "caller-controlled-id"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            nbhd_e2e.build_parser().parse_args(["chat", "history", "--since", "A" * 513])
        parsed = nbhd_e2e.build_parser().parse_args(["receipts", MESSAGE_ID])
        self.assertEqual(parsed.message_id, MESSAGE_ID)

    def test_history_does_not_echo_ids_or_cursor(self):
        response = {
            "messages": [
                {
                    "id": "server-content-id",
                    "client_msg_id": "caller-controlled-id",
                    "source": "app",
                    "created_at": "2026-08-28T00:00:00Z",
                    "text": "private history text",
                }
            ],
            "cursor": "YWJjZA==",
        }
        session = FakeSession([FakeResponse(200, response)])
        client = NBHDClient(session=session)
        client.access_token = "access-token"
        result = client.history(deadline=client.new_deadline(100))
        rendered = json.dumps(result)
        self.assertNotIn("server-content-id", rendered)
        self.assertNotIn("caller-controlled-id", rendered)
        self.assertNotIn("private history text", rendered)
        self.assertNotIn("YWJjZA", rendered)
        self.assertEqual(result["source_counts"], {"app": 1})


class DeadlineTests(unittest.TestCase):
    def test_poll_schedule_boundaries(self):
        self.assertEqual(poll_interval(0), 1.5)
        self.assertEqual(poll_interval(179.999), 1.5)
        self.assertEqual(poll_interval(180), 5.0)
        self.assertEqual(poll_interval(299.999), 5.0)
        self.assertEqual(poll_interval(300), 15.0)

    def test_http_timeout_is_capped_to_absolute_deadline(self):
        clock = FakeClock()
        session = FakeSession([FakeResponse(200, {"messages": [], "cursor": None})])
        client = NBHDClient(session=session, monotonic=clock.monotonic, sleep=clock.sleep)
        client.access_token = "access-token"
        client.history(deadline=102.0)
        self.assertEqual(session.calls[0]["timeout"], 2.0)

    def test_retry_after_beyond_remaining_budget_is_rejected_without_sleep(self):
        clock = FakeClock()
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "999"})])
        client = NBHDClient(session=session, monotonic=clock.monotonic, sleep=clock.sleep)
        client.access_token = "access-token"
        with self.assertRaises(DeadlineExceeded):
            client.history(deadline=105.0)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(session.calls[0]["timeout"], 5.0)

    def test_retry_after_within_budget_is_honored_once(self):
        clock = FakeClock()
        session = FakeSession(
            [
                FakeResponse(429, headers={"Retry-After": "2.5"}),
                FakeResponse(200, {"messages": [], "cursor": None}),
            ]
        )
        client = NBHDClient(session=session, monotonic=clock.monotonic, sleep=clock.sleep)
        client.access_token = "access-token"
        result = client.history(deadline=110.0)
        self.assertEqual(result["count"], 0)
        self.assertEqual(clock.sleeps, [2.5])
        self.assertEqual(len(session.calls), 2)

    def test_poll_sleep_is_capped_to_remaining_deadline(self):
        clock = FakeClock()
        pending = message_shape(receipt=False)
        pending.update({"status": "pending", "reply_text": "", "replied_at": None})
        session = FakeSession([FakeResponse(200, pending)])
        client = NBHDClient(session=session, monotonic=clock.monotonic, sleep=clock.sleep)
        client.access_token = "access-token"
        with self.assertRaises(DeadlineExceeded):
            client.wait_for_message(MESSAGE_ID, deadline=101.0)
        self.assertEqual(clock.sleeps, [1.0])


class ManagedThreadTests(unittest.TestCase):
    def test_create_validation_failure_deletes_recorded_thread(self):
        session = FakeSession(
            [
                FakeResponse(201, {"id": THREAD_ID, "is_main": True}),
                FakeResponse(204),
            ]
        )
        client = NBHDClient(session=session)
        client.access_token = "access-token"
        with (
            self.assertRaises(HarnessError),
            client.managed_thread("fixture", deadline=client.new_deadline(100), cleanup_reporter=lambda: None),
        ):
            self.fail("invalid thread must not be yielded")
        self.assertEqual([call["method"] for call in session.calls], ["POST", "DELETE"])

    def test_cleanup_failure_reports_without_masking_primary(self):
        session = FakeSession(
            [
                FakeResponse(201, {"id": THREAD_ID, "is_main": False}),
                FakeResponse(500),
            ]
        )
        client = NBHDClient(session=session)
        client.access_token = "access-token"
        reports = []
        with (
            self.assertRaisesRegex(HarnessError, "primary failure"),
            client.managed_thread(
                "fixture", deadline=client.new_deadline(100), cleanup_reporter=lambda: reports.append(True)
            ),
        ):
            raise HarnessError("primary failure")
        self.assertEqual(reports, [True])


class FixturePIITests(unittest.TestCase):
    def test_pii_keep_has_no_argument_and_intersects_current_fixture_bindings(self):
        entries = {
            "entries": [
                {"placeholder": "[PERSON_1]", "name": fixtures.PERSON_NAME, "updated_at": None},
                {"placeholder": "[PERSON_2]", "name": "Unrelated Person", "updated_at": None},
            ]
        }
        session = FakeSession(
            [
                FakeResponse(200, entries),
                FakeResponse(200, {"kept": ["[PERSON_1]"], "not_found": []}),
            ]
        )
        client = NBHDClient(session=session)
        client.access_token = "access-token"
        result = client.keep_fixture_pii(deadline=client.new_deadline(100))
        self.assertEqual(result["kept_count"], 1)
        self.assertEqual(session.calls[1]["json"], {"placeholders": ["[PERSON_1]"]})
        parsed = nbhd_e2e.build_parser().parse_args(["pii", "keep"])
        self.assertEqual(parsed.handler, nbhd_e2e._cmd_pii_keep)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            nbhd_e2e.build_parser().parse_args(["pii", "keep", "[PERSON_99]"])


class ReceiptTests(unittest.TestCase):
    def test_receipt_absence_is_feature_detected_and_skipped(self):
        observation = observe_message(message_shape(receipt=False), expected_client_msg_id=MESSAGE_ID)
        events = []
        self.assertFalse(smoke.assert_durable_receipt(observation, lambda *parts: events.append(parts)))
        self.assertEqual(events, [(5, "durable-redaction-receipt", "SKIPPED")])
        smoke.assert_fixture_redactions(observation)


class AtomicKeychainTests(unittest.TestCase):
    @mock.patch("keychain.subprocess.run")
    def test_credential_pair_is_one_json_item_written_via_stdin(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        credentials = keychain.Credentials(access="memory-access", refresh="memory-refresh")
        keychain.write_credentials(credentials)

        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], "-w")
        self.assertIn(keychain.CREDENTIALS_ACCOUNT, argv)
        self.assertNotIn("memory-access", argv)
        self.assertNotIn("memory-refresh", argv)
        self.assertEqual(
            json.loads(run.call_args.kwargs["input"]),
            {"access": "memory-access", "refresh": "memory-refresh"},
        )
        self.assertTrue(run.call_args.kwargs["capture_output"])

    @mock.patch("keychain.subprocess.run")
    def test_atomic_item_read_is_captured_in_memory(self, run):
        payload = json.dumps({"access": "memory-access", "refresh": "memory-refresh"})
        run.return_value = subprocess.CompletedProcess([], 0, f"{payload}\n", "")
        credentials = keychain.read_credentials()
        self.assertEqual(credentials, keychain.Credentials("memory-access", "memory-refresh"))
        self.assertEqual(run.call_count, 1)
        self.assertTrue(run.call_args.kwargs["capture_output"])

    @mock.patch("keychain.subprocess.run")
    def test_legacy_pair_is_migrated_to_one_atomic_item(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 44, "", ""),
            subprocess.CompletedProcess([], 0, "legacy-access\n", ""),
            subprocess.CompletedProcess([], 0, "legacy-refresh\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        credentials = keychain.read_credentials()
        self.assertEqual(credentials, keychain.Credentials("legacy-access", "legacy-refresh"))
        self.assertEqual(run.call_count, 4)
        write_argv = run.call_args_list[-1].args[0]
        self.assertIn(keychain.CREDENTIALS_ACCOUNT, write_argv)
        self.assertEqual(
            json.loads(run.call_args_list[-1].kwargs["input"]),
            {"access": "legacy-access", "refresh": "legacy-refresh"},
        )

    @mock.patch("client.keychain.write_credentials")
    def test_rotated_refresh_is_persisted_with_access_after_gate(self, write_credentials):
        responses = [
            FakeResponse(200, {"access": "new-access", "refresh": "rotated-refresh"}),
            FakeResponse(200, tenant_profile()),
        ]
        with allowlist_file() as path:
            client = NBHDClient(session=FakeSession(responses), allowed_tenants_path=path)
            client.refresh_token = "old-refresh"
            client._refresh_and_gate(deadline=client.new_deadline(100))
        write_credentials.assert_called_once_with(keychain.Credentials(access="new-access", refresh="rotated-refresh"))
        self.assertEqual(client.refresh_token, "rotated-refresh")

    @mock.patch("client.keychain.write_credentials")
    def test_401_refreshes_gates_persists_pair_and_retries_once(self, write_credentials):
        responses = [
            FakeResponse(401),
            FakeResponse(200, {"access": "new-access", "refresh": "rotated-refresh"}),
            FakeResponse(200, tenant_profile()),
            FakeResponse(200, {"messages": [], "cursor": None}),
        ]
        with allowlist_file() as path:
            client = NBHDClient(session=FakeSession(responses), allowed_tenants_path=path)
            client.access_token = "old-access"
            client.refresh_token = "old-refresh"
            result = client.history(deadline=client.new_deadline(100))
        self.assertEqual(result["count"], 0)
        write_credentials.assert_called_once_with(keychain.Credentials(access="new-access", refresh="rotated-refresh"))


if __name__ == "__main__":
    unittest.main()
