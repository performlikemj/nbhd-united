"""Offline cache, retry, helper integration, and default-off parity contracts."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch
from uuid import UUID

from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.core.pipeline.transport import HttpRequest, RequestsTransportResponse
from azure.storage.fileshare._shared.response_handlers import process_storage_error
from django.test import SimpleTestCase, override_settings
from requests import Response

from apps.orchestrator import azure_client
from apps.orchestrator import storage_credentials as credentials

TENANT = "148ccf1c-ef13-47f8-ada1-a98fa90e14a0"
OTHER = "248ccf1c-ef13-47f8-ada1-a98fa90e14a0"
PATH = "workspace/memory/note.md"


def storage_error(status=403, code="AuthenticationFailed"):
    """Use the real SDK's classification, without sending an HTTP request."""
    raw = Response()
    raw.status_code = status
    raw.headers["x-ms-error-code"] = code
    raw._content = b""
    response = RequestsTransportResponse(HttpRequest("PUT", "https://example.file.core.windows.net/share/file"), raw)
    try:
        process_storage_error(HttpResponseError(message="synthetic storage error", response=response))
    except HttpResponseError as exc:
        return exc
    raise AssertionError("SDK did not raise")


def keys(value):
    return SimpleNamespace(keys=[SimpleNamespace(value=value)])


class GateTests(SimpleTestCase):
    def test_gate_truth_table_and_runtime_settings(self):
        for raw, tenant, expected in (
            ("", TENANT, False),
            ("  ,  ", TENANT, False),
            (f" invalid, {TENANT.upper()} , ", TENANT, True),
            (TENANT, TENANT.upper(), True),
            (TENANT, UUID(TENANT), True),
            (TENANT, OTHER, False),
            (TENANT, TENANT[:8], False),
            (TENANT[:8], TENANT[:8], False),
            (TENANT.replace("-", ""), TENANT.replace("-", ""), False),
            ("smoke-deploy", "smoke-deploy", False),
            ("{bad-uuid}", "{bad-uuid}", False),
            (" *, invalid ", TENANT, True),
            ("*", "smoke-deploy", True),
            (None, TENANT, False),
        ):
            with self.subTest(raw=raw, tenant=tenant), override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS=raw):
                self.assertEqual(credentials.storage_key_cache_enabled(tenant), expected)

    def test_ttl_validation(self):
        for value, expected in (
            (1, 1),
            (300, 300),
            (" 42 ", 42),
            (0, 300),
            (-1, 300),
            ("", 300),
            ("invalid", 300),
            ("1.5", 300),
            (1.5, 300),
            (True, 300),
            (False, 300),
            (None, 300),
            ([], 300),
        ):
            with self.subTest(value=value):
                self.assertEqual(credentials.key_cache_ttl_seconds(value), expected)


@override_settings(
    AZURE_STORAGE_KEY_CACHE_TENANT_IDS=TENANT,
    AZURE_STORAGE_KEY_CACHE_TTL_SECONDS="300",
    AZURE_STORAGE_ACCOUNT_NAME=" storage ",
    AZURE_RESOURCE_GROUP="rg",
    AZURE_SUBSCRIPTION_ID="subscription",
)
class CacheTestCase(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch.object(credentials, "_accounts", {}))
        self.enterContext(patch.object(credentials, "_hits", 0))
        self.enterContext(patch.object(credentials, "_bypasses", 0))
        self.enterContext(patch.object(credentials, "_last_summary", 100))
        self.clock = self.enterContext(patch.object(credentials.time, "monotonic", return_value=100))
        self.enterContext(patch.object(azure_client, "_is_mock", return_value=False))
        self.storage = self.enterContext(patch.object(azure_client, "get_storage_client"))
        self.fetch = self.storage.return_value.storage_accounts.list_keys
        self.fetch.return_value = keys("dummy-key-one")


class CacheTests(CacheTestCase):
    def test_miss_hit_and_exact_ttl_boundary(self):
        first = credentials.acquire_account_key(TENANT)
        self.assertTrue(first.cached)
        with self.assertRaises(FrozenInstanceError):
            first.key = "replacement"
        self.clock.return_value = 399.999
        self.assertIs(credentials.acquire_account_key(TENANT), first)
        self.clock.return_value = 400
        self.fetch.return_value = keys("dummy-key-two")
        second = credentials.acquire_account_key(TENANT)
        self.assertEqual(second.key, "dummy-key-two")
        self.assertEqual(second.generation, first.generation + 1)
        self.assertEqual(self.fetch.call_args_list, [call("rg", "storage")] * 2)

    def test_runtime_ttl_override(self):
        for value, ttl in ((7, 7), ("7", 7), ("invalid", 300)):
            with (
                self.subTest(value=value),
                override_settings(AZURE_STORAGE_KEY_CACHE_TTL_SECONDS=value),
                patch.object(credentials, "_accounts", {}),
            ):
                self.clock.return_value = 100
                first = credentials.acquire_account_key(TENANT)
                self.clock.return_value = 100 + ttl - 0.001
                self.assertIs(credentials.acquire_account_key(TENANT), first)
                self.clock.return_value = 100 + ttl
                self.assertGreater(credentials.acquire_account_key(TENANT).generation, first.generation)

    def test_concurrent_cold_burst_fetches_once(self):
        count = 12
        barrier = threading.Barrier(count)
        fetching = threading.Event()
        release = threading.Event()

        def fetch(*args):
            fetching.set()
            if not release.wait(5):
                raise AssertionError("fetch release timed out")
            return keys("dummy-key-one")

        def acquire():
            barrier.wait(timeout=5)
            return credentials.acquire_account_key(TENANT)

        self.fetch.side_effect = fetch
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(acquire) for _ in range(count)]
            try:
                self.assertTrue(fetching.wait(5))
            finally:
                release.set()
            leases = [future.result(timeout=5) for future in futures]
        self.fetch.assert_called_once_with("rg", "storage")
        self.assertTrue(all(lease is leases[0] for lease in leases))

    def test_account_isolation_for_each_dimension(self):
        original = credentials.acquire_account_key(TENANT)
        for setting in ("AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP", "AZURE_STORAGE_ACCOUNT_NAME"):
            with self.subTest(setting=setting), override_settings(**{setting: "other"}):
                other = credentials.acquire_account_key(TENANT)
                self.assertNotEqual(other._account, original._account)
                self.assertIs(credentials.acquire_account_key(TENANT), other)
        self.assertIs(credentials.acquire_account_key(TENANT), original)
        self.assertEqual(self.fetch.call_count, 4)

    def test_tenants_share_the_account_cache(self):
        with override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS="*"):
            lease = credentials.acquire_account_key(TENANT)
            self.assertIs(credentials.acquire_account_key(OTHER), lease)
        self.fetch.assert_called_once()

    def test_bypass_never_reads_or_populates_cache(self):
        original = credentials.acquire_account_key(TENANT)
        with patch.object(credentials, "_accounts", None), patch.object(credentials, "_accounts_lock", None):
            for _ in range(2):
                self.assertFalse(credentials.acquire_account_key(OTHER).cached)
        self.assertIs(credentials.acquire_account_key(TENANT), original)
        self.assertEqual(self.fetch.call_count, 3)

    def test_failed_fetch_not_cached_and_exception_unchanged(self):
        for error in (ResourceNotFoundError("ARM"), ClientAuthenticationError("ARM"), ServiceRequestError("ARM")):
            with self.subTest(error=type(error).__name__):
                self.fetch.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    credentials.acquire_account_key(TENANT)
                self.assertIs(caught.exception, error)
                self.assertTrue(all(state.entry is None for state in credentials._accounts.values()))
        self.fetch.side_effect = None
        self.assertTrue(credentials.acquire_account_key(TENANT).cached)
        self.assertEqual(self.fetch.call_count, 4)

    def test_empty_successes_not_cached(self):
        for value in ("", None):
            with self.subTest(value=value):
                self.fetch.return_value = keys(value)
                lease = credentials.acquire_account_key(TENANT)
                self.assertIs(lease.key, value)
                self.assertFalse(lease.cached)
                self.assertFalse(credentials.acquire_account_key(TENANT).cached)
        self.assertEqual(self.fetch.call_count, 4)

    def test_missing_keys_attribute_and_empty_keys_propagate(self):
        for value, error in ((SimpleNamespace(), AttributeError), (SimpleNamespace(keys=[]), IndexError)):
            with self.subTest(error=error):
                self.fetch.return_value = value
                with self.assertRaises(error):
                    credentials.acquire_account_key(TENANT)
        self.assertEqual(self.fetch.call_count, 2)

    def test_generation_safe_invalidation_reuses_concurrent_refresh(self):
        first = credentials.acquire_account_key(TENANT)
        credentials.invalidate(first)
        self.fetch.return_value = keys("dummy-key-two")
        with ThreadPoolExecutor(max_workers=1) as pool:
            second = pool.submit(credentials.acquire_account_key, TENANT).result(timeout=5)
        credentials.invalidate(first)
        self.assertIs(credentials.acquire_account_key(TENANT), second)
        self.assertEqual(second.generation, first.generation + 1)
        self.assertEqual(self.fetch.call_count, 2)

    def test_bypass_invalidation_does_not_touch_cache(self):
        lease = credentials.acquire_account_key(OTHER)
        with patch.object(credentials, "_accounts", None):
            credentials.invalidate(lease)


class RetryTests(CacheTestCase):
    def test_cached_auth_failure_retries_once_with_new_key(self):
        credentials.acquire_account_key(TENANT)
        self.fetch.return_value = keys("dummy-key-two")
        op = MagicMock(side_effect=[storage_error(), "result"])
        self.assertEqual(credentials.run_with_key(TENANT, op), "result")
        self.assertEqual(op.call_args_list, [call("dummy-key-one"), call("dummy-key-two")])
        self.assertEqual(self.fetch.call_count, 2)

    def test_cold_cached_success_can_refresh(self):
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]
        op = MagicMock(side_effect=[storage_error(), None])
        credentials.run_with_key(TENANT, op)
        self.assertEqual(self.fetch.call_count, 2)

    def test_non_cached_lease_does_not_retry(self):
        error = storage_error()
        op = MagicMock(side_effect=error)
        with self.assertRaises(ClientAuthenticationError) as caught:
            credentials.run_with_key(OTHER, op)
        self.assertIs(caught.exception, error)
        op.assert_called_once_with("dummy-key-one")
        self.fetch.assert_called_once()

    def test_other_errors_do_not_retry(self):
        errors = [
            storage_error(code="AuthorizationPermissionMismatch"),
            ServiceRequestError("network"),
            ClientAuthenticationError("no error code"),
            HttpResponseError("generic"),
        ]
        wrong_code = ClientAuthenticationError("wrong code")
        wrong_code.error_code = "AuthorizationPermissionMismatch"
        errors.append(wrong_code)
        for error in errors:
            with self.subTest(error=type(error).__name__):
                op = MagicMock(side_effect=error)
                with self.assertRaises(type(error)) as caught:
                    credentials.run_with_key(TENANT, op)
                self.assertIs(caught.exception, error)
                op.assert_called_once()
        self.fetch.assert_called_once()

    def test_second_failure_propagates(self):
        first, second = storage_error(), storage_error()
        op = MagicMock(side_effect=[first, second])
        with self.assertRaises(ClientAuthenticationError) as caught:
            credentials.run_with_key(TENANT, op)
        self.assertIs(caught.exception, second)
        self.assertEqual(op.call_count, 2)
        self.assertEqual(self.fetch.call_count, 2)

    def test_refresh_acquisition_failure_propagates_without_replaying_operation(self):
        error = ResourceNotFoundError("ARM refresh")
        self.fetch.side_effect = [keys("dummy-key-one"), error]
        op = MagicMock(side_effect=storage_error())
        with self.assertRaises(ResourceNotFoundError) as caught:
            credentials.run_with_key(TENANT, op)
        self.assertIs(caught.exception, error)
        op.assert_called_once()

    def test_retry_reuses_refresh_completed_by_another_thread(self):
        old = credentials.acquire_account_key(TENANT)
        self.fetch.return_value = keys("dummy-key-two")

        def fail_after_refresh(key):
            credentials.invalidate(old)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(credentials.acquire_account_key, TENANT).result(timeout=5)
            raise storage_error()

        op = MagicMock(side_effect=[None])
        op.side_effect = lambda key: fail_after_refresh(key) if key == old.key else "success"
        self.assertEqual(credentials.run_with_key(TENANT, op), "success")
        self.assertEqual(self.fetch.call_count, 2)


class HelperTests(CacheTestCase):
    def setUp(self):
        super().setUp()
        self.file_cls = self.enterContext(patch("azure.storage.fileshare.ShareFileClient"))
        self.dir_cls = self.enterContext(patch("azure.storage.fileshare.ShareDirectoryClient"))
        self.clients = {key: MagicMock() for key in ("dummy-key-one", "dummy-key-two")}
        self.file_cls.side_effect = lambda **kwargs: self.clients[kwargs["credential"]]
        self.first = self.clients["dummy-key-one"]
        self.second = self.clients["dummy-key-two"]
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]

    def test_download_arm_failure_propagates(self):
        for error in (ResourceNotFoundError("ARM"), storage_error(), ServiceRequestError("ARM")):
            with self.subTest(error=type(error).__name__):
                self.fetch.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    azure_client.download_workspace_file(TENANT, PATH)
                self.assertIs(caught.exception, error)
        self.file_cls.assert_not_called()

    def test_download_data_plane_not_found_returns_none(self):
        self.first.download_file.side_effect = ResourceNotFoundError("file")
        self.assertIsNone(azure_client.download_workspace_file(TENANT, PATH))
        self.fetch.assert_called_once()

    def test_download_readall_not_found_returns_none(self):
        self.first.download_file.return_value.readall.side_effect = ResourceNotFoundError("file")
        self.assertIsNone(azure_client.download_workspace_file(TENANT, PATH))

    def test_download_constructor_not_found_propagates(self):
        error = ResourceNotFoundError("constructor")
        self.file_cls.side_effect = error
        with self.assertRaises(ResourceNotFoundError) as caught:
            azure_client.download_workspace_file(TENANT, PATH)
        self.assertIs(caught.exception, error)

    def test_download_readall_auth_failure_retries_and_preserves_decoding(self):
        self.first.download_file.return_value.readall.side_effect = storage_error()
        self.second.download_file.return_value.readall.return_value = b"caf\xc3\xa9\xff"
        self.assertEqual(azure_client.download_workspace_file(TENANT, PATH), "café\ufffd")
        self.assertEqual(self.fetch.call_count, 2)
        for client in self.clients.values():
            client.download_file.assert_called_once_with()
            client.download_file.return_value.readall.assert_called_once_with()
        self.assertEqual(
            self.file_cls.call_args_list,
            [
                call(
                    account_url="https://storage.file.core.windows.net",
                    share_name=f"ws-{TENANT[:20]}",
                    file_path=PATH,
                    credential=key,
                )
                for key in self.clients
            ],
        )

    def test_mock_branches_do_not_acquire_or_construct_clients(self):
        with (
            patch.object(azure_client, "_is_mock", return_value=True),
            patch.object(credentials, "acquire_account_key", side_effect=AssertionError("unexpected acquire")),
            self.assertLogs(azure_client.logger, level="INFO") as logs,
        ):
            self.assertIsNone(azure_client.download_workspace_file(TENANT, PATH))
            self.assertIsNone(azure_client.upload_workspace_file(TENANT, PATH, "text"))
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [
                f"[MOCK] Download of {PATH} from file share ws-{TENANT[:20]}",
                f"[MOCK] Uploaded {PATH} to file share ws-{TENANT[:20]}",
            ],
        )
        self.storage.assert_not_called()
        self.file_cls.assert_not_called()

    def test_upload_preserves_skip_decision_and_resends_full_sanitized_payload(self):
        self.first.get_file_properties.side_effect = ResourceNotFoundError("absent")
        self.first.upload_file.side_effect = storage_error()
        with self.assertLogs(azure_client.logger, level="INFO") as logs:
            azure_client.upload_workspace_file(TENANT, PATH, "café\x00\x01\n\ttext", skip_if_exists=True)
        payload = "café\n\ttext".encode()
        self.first.get_file_properties.assert_called_once_with()
        self.second.get_file_properties.assert_not_called()
        self.first.upload_file.assert_called_once_with(payload, length=len(payload))
        self.second.upload_file.assert_called_once_with(payload, length=len(payload))
        self.assertIs(self.first.upload_file.call_args.args[0], self.second.upload_file.call_args.args[0])
        self.assertEqual(
            logs.records[-1].getMessage(), f"Uploaded {PATH} ({len(payload)} bytes) to file share ws-{TENANT[:20]}"
        )

    def test_existence_check_auth_retry_can_skip_without_upload(self):
        self.first.get_file_properties.side_effect = storage_error()
        azure_client.upload_workspace_file(TENANT, PATH, "text", skip_if_exists=True)
        self.first.get_file_properties.assert_called_once()
        self.second.get_file_properties.assert_called_once()
        self.first.upload_file.assert_not_called()
        self.second.upload_file.assert_not_called()
        self.assertEqual(self.fetch.call_count, 2)

    def test_existence_check_refresh_is_used_for_upload(self):
        self.first.get_file_properties.side_effect = storage_error()
        self.second.get_file_properties.side_effect = ResourceNotFoundError("absent")
        azure_client.upload_workspace_file(TENANT, PATH, "text", skip_if_exists=True)
        self.first.upload_file.assert_not_called()
        self.second.upload_file.assert_called_once_with(b"text", length=4)
        self.assertEqual(self.fetch.call_count, 2)

    def test_parent_repair_works_after_auth_refresh(self):
        self.first.upload_file.side_effect = storage_error()
        self.second.upload_file.side_effect = [storage_error(404, "ParentNotFound"), None]
        self.dir_cls.return_value.create_directory.side_effect = [storage_error(409, "ResourceAlreadyExists"), None]
        azure_client.upload_workspace_file(TENANT, PATH, "text")
        self.assertEqual(self.second.upload_file.call_args_list, [call(b"text", length=4)] * 2)
        self.assertEqual(
            self.dir_cls.call_args_list,
            [
                call(
                    account_url="https://storage.file.core.windows.net",
                    share_name=f"ws-{TENANT[:20]}",
                    directory_path=path,
                    credential="dummy-key-two",
                )
                for path in ("workspace", "workspace/memory")
            ],
        )

    def test_directory_auth_failure_rebuilds_clients_and_resends_payload(self):
        self.first.upload_file.side_effect = storage_error(404, "ParentNotFound")
        self.second.upload_file.side_effect = [storage_error(404, "ParentNotFound"), None]
        self.dir_cls.return_value.create_directory.side_effect = [storage_error(), None, None]
        azure_client.upload_workspace_file(TENANT, PATH, "text")
        self.assertEqual(
            [c.kwargs["credential"] for c in self.dir_cls.call_args_list],
            ["dummy-key-one", "dummy-key-two", "dummy-key-two"],
        )
        self.assertEqual(self.second.upload_file.call_args_list, [call(b"text", length=4)] * 2)

    def test_ensure_dirs_false_retries_full_binary_payload(self):
        payload = b"\x00\xffbinary"
        self.first.upload_file.side_effect = storage_error()
        azure_client._put_share_file(TENANT, PATH, data=payload, ensure_dirs=False)
        self.first.upload_file.assert_called_once_with(payload, length=len(payload))
        self.second.upload_file.assert_called_once_with(payload, length=len(payload))
        self.dir_cls.assert_not_called()

    def test_validation_precedes_key_acquisition(self):
        for kwargs in ({}, {"text": "text", "data": b"data"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                azure_client._put_share_file(TENANT, PATH, **kwargs)
        with override_settings(AZURE_STORAGE_ACCOUNT_NAME=" "):
            with self.assertRaisesMessage(ValueError, "AZURE_STORAGE_ACCOUNT_NAME is not configured"):
                azure_client.upload_workspace_file(TENANT, PATH, "text")
            with self.assertRaisesMessage(ValueError, "AZURE_STORAGE_ACCOUNT_NAME is not configured"):
                azure_client.download_workspace_file(TENANT, PATH)
        self.storage.assert_not_called()

    @override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS="")
    def test_default_off_fetch_counts_results_and_skip_decision(self):
        self.fetch.side_effect = None
        self.first.download_file.return_value.readall.return_value = b"text\xff"
        self.first.get_file_properties.side_effect = ResourceNotFoundError("absent")
        for _ in range(2):
            self.assertEqual(azure_client.download_workspace_file(TENANT, PATH), "text\ufffd")
            self.assertIsNone(azure_client.upload_workspace_file(TENANT, PATH, "text", skip_if_exists=True))
        self.assertEqual(self.fetch.call_args_list, [call("rg", "storage")] * 4)
        self.assertEqual(self.first.get_file_properties.call_count, 2)
        self.assertEqual(self.first.upload_file.call_args_list, [call(b"text", length=4)] * 2)
        self.assertEqual(credentials._accounts, {})
        self.first.get_file_properties.side_effect = None
        azure_client.upload_workspace_file(TENANT, PATH, "text", skip_if_exists=True)
        self.assertEqual(self.fetch.call_count, 5)
        self.assertEqual(self.first.upload_file.call_count, 2)

    @override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS="")
    def test_default_off_data_errors_unchanged_and_no_auth_retry(self):
        self.fetch.side_effect = None
        for error in (
            storage_error(),
            storage_error(code="AuthorizationPermissionMismatch"),
            ServiceRequestError("network"),
        ):
            for operation, method in (
                (lambda: azure_client.download_workspace_file(TENANT, PATH), self.first.download_file),
                (lambda: azure_client.upload_workspace_file(TENANT, PATH, "text"), self.first.upload_file),
            ):
                with self.subTest(error=type(error).__name__, method=method._mock_name):
                    method.reset_mock()
                    method.side_effect = error
                    before = self.fetch.call_count
                    with self.assertRaises(type(error)) as caught:
                        operation()
                    self.assertIs(caught.exception, error)
                    method.assert_called_once()
                    self.assertEqual(self.fetch.call_count, before + 1)

    @override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS="")
    def test_default_off_arm_and_missing_file_parity(self):
        error = ResourceNotFoundError("ARM")
        self.fetch.side_effect = error
        for operation in (
            lambda: azure_client.download_workspace_file(TENANT, PATH),
            lambda: azure_client.upload_workspace_file(TENANT, PATH, "text"),
        ):
            with self.assertRaises(ResourceNotFoundError) as caught:
                operation()
            self.assertIs(caught.exception, error)
        self.fetch.side_effect = None
        self.first.download_file.side_effect = ResourceNotFoundError("file")
        self.assertIsNone(azure_client.download_workspace_file(TENANT, PATH))
        self.assertEqual(self.fetch.call_count, 3)


class TelemetryTests(CacheTestCase):
    def test_fetch_sources_fields_and_no_key_material(self):
        records = []

        def record(message, *args):
            self.assertFalse(credentials._accounts_lock.locked())
            self.assertFalse(credentials._summary_lock.locked())
            self.assertTrue(all(not state.lock.locked() for state in credentials._accounts.values()))
            records.append(message % args)

        with (
            patch.object(credentials.logger, "info", side_effect=record),
            patch.object(credentials.os, "getpid", return_value=123),
            patch.object(credentials, "_PROCESS_START", 456),
            patch.object(credentials.os.environ, "get", return_value="replica-test") as replica,
        ):
            first = credentials.acquire_account_key(TENANT)
            credentials.acquire_account_key(TENANT)
            credentials.acquire_account_key(OTHER)
            self.clock.return_value = 400
            second = credentials.acquire_account_key(TENANT)
            credentials.invalidate(second)
            credentials.acquire_account_key(TENANT)
        self.assertEqual(len(records), 3)
        for message, source in zip(records, ("cold", "expired", "auth_refresh")):
            self.assertEqual(
                message, f"storage_key fetch source={source} tenant={TENANT} replica=replica-test proc=123-456 ms=0.000"
            )
            self.assertNotIn(first.key, message)
        self.assertNotIn(first.key, repr(first))
        self.assertEqual(replica.call_args_list, [call("CONTAINER_APP_REPLICA_NAME")] * 3)

    @override_settings(AZURE_STORAGE_KEY_CACHE_TTL_SECONDS="3600")
    def test_no_per_hit_or_bypass_info_and_summary_rate_limit(self):
        credentials.acquire_account_key(TENANT)

        def assert_unlocked_and_reset(*args):
            self.assertFalse(credentials._accounts_lock.locked())
            self.assertFalse(credentials._summary_lock.locked())
            self.assertTrue(all(not state.lock.locked() for state in credentials._accounts.values()))
            self.assertEqual((credentials._hits, credentials._bypasses), (0, 0))

        message = "storage_key cache summary hits=%d bypasses=%d replica=%s proc=%d-%d"
        with (
            patch.object(credentials.logger, "info", side_effect=assert_unlocked_and_reset) as log,
            patch.object(credentials.os, "getpid", return_value=123),
            patch.object(credentials, "_PROCESS_START", 456),
            patch.object(credentials.os.environ, "get", return_value="replica-test") as replica,
        ):
            for _ in range(3):
                credentials.acquire_account_key(TENANT)
                credentials.acquire_account_key(OTHER)
            log.assert_not_called()
            self.clock.return_value = 700
            credentials.acquire_account_key(OTHER)
            log.assert_called_once_with(message, 3, 4, "replica-test", 123, 456)
            self.clock.return_value = 1299.999
            credentials.acquire_account_key(TENANT)
            credentials.acquire_account_key(OTHER)
            self.assertEqual(log.call_count, 1)
            replica.return_value = None
            self.clock.return_value = 1300
            credentials.acquire_account_key(OTHER)
            self.assertEqual(log.call_args, call(message, 1, 2, "-", 123, 456))
            self.assertEqual(log.call_count, 2)
            self.assertEqual(replica.call_args_list, [call("CONTAINER_APP_REPLICA_NAME")] * 2)

    def test_failed_fetch_logs_no_exception_body_and_replica_fallback(self):
        self.fetch.side_effect = ClientAuthenticationError("dummy-secret-error-body")
        with (
            self.assertLogs(credentials.logger, level="INFO") as logs,
            patch.object(credentials.os.environ, "get", return_value=None),
            self.assertRaises(ClientAuthenticationError),
        ):
            credentials.acquire_account_key(TENANT)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("replica=-", logs.records[0].getMessage())
        for record in logs.records:
            self.assertNotIn("dummy-secret", record.getMessage())
            self.assertNotIn("dummy-key", record.getMessage())
            self.assertIsNone(record.exc_info)
