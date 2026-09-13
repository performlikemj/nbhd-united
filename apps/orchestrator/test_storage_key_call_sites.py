"""Offline contracts for the remaining storage-key cache consumers."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ResourceNotFoundError
from django.test import RequestFactory, override_settings

from apps.integrations import services as integrations
from apps.orchestrator import azure_client, memory_sync
from apps.orchestrator import storage_credentials as credentials
from apps.orchestrator.test_storage_credentials import OTHER, TENANT, CacheTestCase, keys, storage_error
from apps.router import pending_queue, tasks, views
from apps.router.poller import TelegramPoller


@override_settings(AZURE_CONTAINER_ENV_ID="/managedEnvironments/test-env")
class MigratedShareCallSiteTests(CacheTestCase):
    sites = (
        "gws_write",
        "gws_delete",
        "config",
        "delete",
        "binary",
        "register",
        "memory",
        "queue_photo",
        "poller_photo",
        "chart",
        "audio",
    )

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"AZURE_MOCK": "false"}))
        self.enterContext(patch.object(pending_queue, "suppresses_real_transport", return_value=False))
        self.enterContext(patch("apps.router.poller.suppresses_real_transport", return_value=False))
        self.enterContext(patch.object(pending_queue, "_telegram_api_base", return_value="https://example.invalid"))
        self.post = self.enterContext(patch.object(pending_queue.httpx, "post"))
        self.post.return_value.is_success = True
        self.poller = TelegramPoller()
        self.poller._http = MagicMock()
        self.poller._http.post.return_value.is_success = True
        self.file_cls = self.enterContext(patch("azure.storage.fileshare.ShareFileClient"))
        self.share_cls = self.enterContext(patch("azure.storage.fileshare.ShareClient"))
        self.dir_cls = self.enterContext(patch("azure.storage.fileshare.ShareDirectoryClient"))
        self.container = self.enterContext(patch.object(azure_client, "get_container_client"))
        self.first = MagicMock()
        self.second = MagicMock()
        self.clients = {"dummy-key-one": self.first, "dummy-key-two": self.second}
        self.file_cls.side_effect = lambda **kw: self.clients[kw["credential"]]
        self.share_cls.side_effect = lambda **kw: self.clients[kw["credential"]]
        for client in self.clients.values():
            client.download_file.return_value.readall.return_value = b"\x00\xffdata"
            client.get_file_properties.return_value.size = 0
        self.request = RequestFactory().get("/", HTTP_RANGE="bytes=0-1")

    def operation(self, site, tenant_id=TENANT):
        tenant = SimpleNamespace(id=tenant_id)
        operations = {
            "gws_write": lambda: integrations._write_gws_credentials_to_file_share(
                tenant, {"refresh_token": "test-only-token"}
            ),
            "gws_delete": lambda: integrations._delete_gws_credentials_from_file_share(tenant),
            "config": lambda: azure_client.download_config_from_file_share(tenant_id),
            "delete": lambda: azure_client.delete_workspace_file(tenant_id, "workspace/note.md"),
            "binary": lambda: azure_client.download_workspace_file_binary(tenant_id, "workspace/photo.png"),
            "register": lambda: azure_client.register_environment_storage(tenant_id),
            "memory": lambda: memory_sync.upload_memory_files_to_share(tenant_id, {"memory/note.md": "a\x00b"}),
            "queue_photo": lambda: pending_queue._send_telegram_photo(
                42, "/home/node/.openclaw/workspace/photo.png", tenant
            ),
            "poller_photo": lambda: self.poller._send_photo(42, "/home/node/.openclaw/workspace/photo.png", tenant),
            "chart": lambda: views.serve_chart_image(self.request, tenant_id, "chart.png"),
            "audio": lambda: views.serve_meditation_audio(self.request, tenant_id, "audio.mp3"),
        }
        return operations[site]()

    def assert_result(self, site, result):
        if site in ("config", "binary"):
            self.assertEqual(result, b"\x00\xffdata")
        elif site in ("queue_photo", "poller_photo"):
            self.assertIs(result, True)
        elif site == "memory":
            self.assertEqual(result, 1)
        elif site == "chart":
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.content, b"\x00\xffdata")
            self.assertEqual(result["Content-Type"], "image/png")
            self.assertEqual(result["Cache-Control"], "public, max-age=3600")
        elif site == "audio":
            self.assertEqual(result.status_code, 206)
            self.assertEqual(result.content, b"\x00\xff")
            self.assertEqual(result["Content-Range"], "bytes 0-1/6")
        else:
            self.assertIsNone(result)

    def test_repeat_calls_reuse_one_fetch_for_each_site(self):
        for gate in ("*", TENANT):
            for site in self.sites:
                with self.subTest(gate=gate, site=site), override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS=gate):
                    credentials._accounts.clear()
                    self.fetch.reset_mock()
                    for _ in range(2):
                        self.assert_result(site, self.operation(site))
                    self.fetch.assert_called_once_with("rg", "storage")

    def test_disabled_and_excluded_tenants_fetch_per_call_even_with_warm_cache(self):
        credentials.acquire_account_key(TENANT)
        for gate in ("", OTHER):
            for site in self.sites:
                with self.subTest(gate=gate, site=site), override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS=gate):
                    self.fetch.reset_mock()
                    for _ in range(2):
                        self.assert_result(site, self.operation(site))
                    self.assertEqual(self.fetch.call_args_list, [call("rg", "storage")] * 2)

    def failure_method(self, site, client):
        if site in ("gws_write", "memory"):
            return client.upload_file
        if site in ("gws_delete", "delete"):
            return client.delete_file
        if site == "register":
            return self.container.return_value.managed_environments_storages.create_or_update
        return client.download_file.return_value.readall

    def test_auth_refresh_rebuilds_clients_and_keeps_results(self):
        for site in self.sites:
            with self.subTest(site=site):
                credentials._accounts.clear()
                self.fetch.reset_mock()
                self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]
                method = self.failure_method(site, self.first)
                method.side_effect = [storage_error(), None] if site == "register" else storage_error()
                self.post.reset_mock()
                self.poller._http.post.reset_mock()
                try:
                    self.assert_result(site, self.operation(site))
                finally:
                    method.side_effect = None
                self.assertEqual(self.fetch.call_count, 2)
                if site == "queue_photo":
                    self.post.assert_called_once()
                elif site == "poller_photo":
                    self.poller._http.post.assert_called_once()

    def test_gate_off_auth_failure_does_not_retry(self):
        for gate in ("", OTHER):
            for site in self.sites:
                with self.subTest(gate=gate, site=site), override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS=gate):
                    self.fetch.reset_mock()
                    method = self.failure_method(site, self.first)
                    method.reset_mock()
                    method.side_effect = storage_error()
                    try:
                        # These consumers intentionally translate errors to a response/no-op.
                        if site in ("gws_delete", "queue_photo", "poller_photo", "chart", "audio"):
                            with patch("logging.Logger.exception"):
                                result = self.operation(site)
                            if site in ("chart", "audio"):
                                self.assertEqual(result.status_code, 404)
                            elif site != "gws_delete":
                                self.assertIs(result, False)
                        else:
                            with self.assertRaises(ClientAuthenticationError):
                                self.operation(site)
                    finally:
                        method.side_effect = None
                    method.assert_called_once()
                    self.fetch.assert_called_once()

    def test_single_stage_second_auth_failure_is_not_retried_again(self):
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]
        self.first.download_file.side_effect = storage_error()
        self.second.download_file.side_effect = storage_error()
        with self.assertRaises(ClientAuthenticationError):
            self.operation("binary")
        self.assertEqual(self.fetch.call_count, 2)
        self.first.download_file.assert_called_once()
        self.second.download_file.assert_called_once()

    def test_reads_keep_not_found_semantics(self):
        self.first.download_file.return_value.readall.side_effect = ResourceNotFoundError("absent")
        for site in ("config", "binary"):
            with self.subTest(site=site):
                self.assertIsNone(self.operation(site))
        self.fetch.assert_called_once()

    def test_delete_retains_refresh_from_properties(self):
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]
        self.first.get_file_properties.side_effect = storage_error()
        self.operation("delete")
        self.first.delete_file.assert_not_called()
        self.second.delete_file.assert_called_once()
        self.assertEqual(self.fetch.call_count, 2)

    def test_delete_absent_does_not_delete(self):
        self.first.get_file_properties.side_effect = ResourceNotFoundError("absent")
        self.operation("delete")
        self.first.delete_file.assert_not_called()
        self.fetch.assert_called_once()

    def test_memory_retains_refresh_from_share_check_across_files(self):
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]
        self.first.get_share_properties.side_effect = storage_error()
        self.assertEqual(
            memory_sync.upload_memory_files_to_share(TENANT, {"memory/a.md": "a\x00b", "memory/b.md": "c"}), 2
        )
        self.first.get_file_properties.assert_not_called()
        self.first.upload_file.assert_not_called()
        self.assertEqual(self.second.upload_file.call_args_list, [call(b"ab", length=2), call(b"c", length=1)])
        self.assertEqual(self.fetch.call_count, 2)

    def test_memory_retains_refresh_from_content_check_for_upload(self):
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]
        self.first.get_file_properties.side_effect = storage_error()
        self.assertEqual(self.operation("memory"), 1)
        self.first.upload_file.assert_not_called()
        self.second.upload_file.assert_called_once_with(b"ab", length=2)
        self.assertEqual(self.fetch.call_count, 2)

    def test_memory_missing_file_after_refresh_keeps_new_lease_for_upload(self):
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]
        self.first.get_file_properties.side_effect = storage_error()
        self.second.get_file_properties.side_effect = ResourceNotFoundError("absent")
        self.assertEqual(self.operation("memory"), 1)
        self.first.upload_file.assert_not_called()
        self.second.upload_file.assert_called_once_with(b"ab", length=2)
        self.assertEqual(self.fetch.call_count, 2)

    def test_memory_retains_upload_refresh_for_next_file_and_parent_repair(self):
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]
        self.first.upload_file.side_effect = storage_error()
        self.second.upload_file.side_effect = [storage_error(404, "ParentNotFound"), None, None]
        files = {"memory/a.md": "a\x00b", "memory/b.md": "c"}
        self.assertEqual(memory_sync.upload_memory_files_to_share(TENANT, files), 2)
        self.first.get_file_properties.assert_called_once()
        self.first.upload_file.assert_called_once_with(b"ab", length=2)
        self.assertEqual(self.second.upload_file.call_args_list, [call(b"ab", length=2)] * 2 + [call(b"c", length=1)])
        self.assertTrue(all(c.kwargs["credential"] == "dummy-key-two" for c in self.dir_cls.call_args_list))
        self.assertEqual(self.fetch.call_count, 2)

    def test_registration_does_not_retry_generic_arm_403(self):
        method = self.container.return_value.managed_environments_storages.create_or_update
        error = HttpResponseError("synthetic ARM permission failure")
        error.status_code = 403
        method.side_effect = error
        with self.assertRaises(HttpResponseError) as caught:
            self.operation("register")
        self.assertIs(caught.exception, error)
        method.assert_called_once()
        self.fetch.assert_called_once()


class CleanupKeyCacheTests(CacheTestCase):
    def setUp(self):
        super().setUp()
        self.tenants = [SimpleNamespace(id=TENANT), SimpleNamespace(id=OTHER)]
        tenant_model = self.enterContext(patch.object(tasks, "Tenant"))
        queryset = tenant_model.objects.filter.return_value.exclude.return_value
        queryset.__iter__.side_effect = lambda: iter(self.tenants)
        queryset.count.return_value = len(self.tenants)
        self.dir_cls = self.enterContext(patch("azure.storage.fileshare.ShareDirectoryClient"))
        self.first, self.second = MagicMock(), MagicMock()
        clients = {"dummy-key-one": self.first, "dummy-key-two": self.second}
        self.dir_cls.side_effect = lambda **kw: clients[kw["credential"]]
        for directory in clients.values():
            directory.list_directories_and_files.return_value = [
                {"name": "old.pdf", "is_directory": False},
                {"name": "subdir", "is_directory": True},
            ]
            directory.get_file_client.return_value.get_file_properties.return_value.last_modified = datetime.now(
                UTC
            ) - timedelta(hours=48)

    @override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS="*")
    def test_repeat_tasks_fetch_once_across_tenants(self):
        tasks.cleanup_inbound_media_task()
        tasks.cleanup_inbound_media_task()
        self.fetch.assert_called_once()
        self.assertEqual(self.first.get_file_client.return_value.delete_file.call_count, 4)

    @override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS="")
    def test_gate_off_preserves_one_fetch_per_task_not_per_tenant(self):
        tasks.cleanup_inbound_media_task()
        tasks.cleanup_inbound_media_task()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.first.get_file_client.return_value.delete_file.call_count, 4)
        self.assertEqual(credentials._accounts, {})

    def test_allowlist_uses_uncached_baseline_for_excluded_tenants(self):
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two"), keys("dummy-key-one")]
        for _ in range(2):
            tasks.cleanup_inbound_media_task()
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual(self.first.get_file_client.return_value.delete_file.call_count, 2)
        self.assertEqual(self.second.get_file_client.return_value.delete_file.call_count, 2)
        self.assertEqual(
            [(c.kwargs["share_name"], c.kwargs["credential"] == "dummy-key-two") for c in self.dir_cls.call_args_list],
            [(f"ws-{TENANT[:20]}", True), (f"ws-{OTHER[:20]}", False)] * 2,
        )

    @override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS="*")
    def test_refreshed_listing_lease_reaches_properties_delete_and_next_tenant(self):
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]
        self.first.list_directories_and_files.side_effect = storage_error()
        tasks.cleanup_inbound_media_task()
        self.first.get_file_client.assert_not_called()
        self.assertEqual(self.second.get_file_client.return_value.delete_file.call_count, 2)
        self.assertEqual(self.fetch.call_count, 2)

    @override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS="*")
    def test_refreshed_properties_lease_reaches_delete(self):
        self.fetch.side_effect = [keys("dummy-key-one"), keys("dummy-key-two")]
        self.first.get_file_client.return_value.get_file_properties.side_effect = storage_error()
        tasks.cleanup_inbound_media_task()
        self.first.get_file_client.return_value.delete_file.assert_not_called()
        self.assertEqual(self.second.get_file_client.return_value.delete_file.call_count, 2)
        self.assertEqual(self.fetch.call_count, 2)

    @override_settings(AZURE_STORAGE_KEY_CACHE_TENANT_IDS="")
    def test_excluded_auth_failure_does_not_retry(self):
        self.first.list_directories_and_files.side_effect = storage_error()
        with self.assertLogs(tasks.logger, level="WARNING"):
            tasks.cleanup_inbound_media_task()
        self.assertEqual(self.first.list_directories_and_files.call_count, 2)
        self.first.get_file_client.assert_not_called()
        self.fetch.assert_called_once()
