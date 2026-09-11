"""Tests for memory_sync module."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, call, patch

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.fileshare import StorageErrorCode
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.journal.models import Document
from apps.orchestrator.memory_sync import render_memory_files, upload_memory_files_to_share
from apps.tenants.services import create_tenant


def _storage_error(error_type, code):
    error = error_type("private response body")
    error.error_code = code
    return error


@override_settings(AZURE_STORAGE_ACCOUNT_NAME="storage", AZURE_RESOURCE_GROUP="rg")
class UploadMemoryFilesTest(SimpleTestCase):
    tenant_id = "tenant"
    first_path = "memory/journal/daily/first.md"
    second_path = "memory/journal/daily/second.md"
    parents = ("memory", "memory/journal", "memory/journal/daily")

    def setUp(self):
        self.enterContext(patch("apps.orchestrator.azure_client._is_mock", return_value=False))
        storage = self.enterContext(patch("apps.orchestrator.azure_client.get_storage_client"))
        keys = MagicMock()
        keys.keys = [MagicMock(value="dummy-key")]
        storage.return_value.storage_accounts.list_keys.return_value = keys
        self.enterContext(patch("azure.storage.fileshare.ShareClient"))
        self.dir_cls = self.enterContext(patch("azure.storage.fileshare.ShareDirectoryClient"))
        self.file_cls = self.enterContext(patch("azure.storage.fileshare.ShareFileClient"))
        self.files = {self.first_path: "first", self.second_path: "second"}
        self.clients = {path: MagicMock() for path in self.files}
        self.file_cls.side_effect = lambda **kwargs: self.clients[kwargs["file_path"]]
        self.directories = {path: MagicMock() for path in self.parents}
        self.dir_cls.side_effect = lambda **kwargs: self.directories[kwargs["directory_path"]]
        self.operations = MagicMock()
        for index, client in enumerate(self.clients.values(), start=1):
            client.get_file_properties.side_effect = _storage_error(ResourceNotFoundError, "ResourceNotFound")
            self.operations.attach_mock(client.get_file_properties, f"check_{index}")
            self.operations.attach_mock(client.upload_file, f"upload_{index}")
        for depth, directory in enumerate(self.directories.values(), start=1):
            self.operations.attach_mock(directory.create_directory, f"create_{depth}")

    def _missing_parent(self):
        return _storage_error(ResourceNotFoundError, StorageErrorCode.PARENT_NOT_FOUND)

    def _assert_no_creates(self):
        self.dir_cls.assert_not_called()
        for directory in self.directories.values():
            directory.create_directory.assert_not_called()

    def _assert_warning(self, captured, kind, error_type, code):
        self.assertEqual(
            captured.output,
            [
                f"WARNING:apps.orchestrator.memory_sync:memory_sync: {kind} for "
                f"ws-{self.tenant_id}/{self.first_path}: type={error_type} code={code} — skipping file"
            ],
        )
        self.assertIsNone(captured.records[0].exc_info)

    def test_all_unchanged_skip_uploads_and_directory_calls(self):
        self.files[self.first_path] = "first\x00\x01"
        for path, client in self.clients.items():
            encoded = b"first" if path == self.first_path else b"second"
            client.get_file_properties.side_effect = None
            client.get_file_properties.return_value.size = len(encoded)
            client.download_file.return_value.readall.return_value = encoded

        self.assertEqual(upload_memory_files_to_share(self.tenant_id, self.files), 0)

        self._assert_no_creates()
        for client in self.clients.values():
            client.get_file_properties.assert_called_once_with()
            client.download_file.assert_called_once_with()
            client.upload_file.assert_not_called()

    def test_changed_file_uploads_once_without_directory_calls(self):
        client = self.clients[self.first_path]
        client.get_file_properties.side_effect = None
        for previous in (b"older", b"different size"):
            with self.subTest(previous=previous):
                client.reset_mock()
                self.operations.reset_mock()
                client.get_file_properties.return_value.size = len(previous)
                client.download_file.return_value.readall.return_value = previous

                self.assertEqual(upload_memory_files_to_share(self.tenant_id, {self.first_path: "first"}), 1)

                self._assert_no_creates()
                client.upload_file.assert_called_once_with(b"first", length=5)
                self.assertEqual(self.operations.mock_calls, [call.check_1(), call.upload_1(b"first", length=5)])
                if len(previous) == 5:
                    client.download_file.assert_called_once_with()
                else:
                    client.download_file.assert_not_called()

    def test_missing_parents_created_in_order_before_one_retry(self):
        client = self.clients[self.first_path]
        client.get_file_properties.side_effect = self._missing_parent()
        client.upload_file.side_effect = [self._missing_parent(), None]
        payload = "café\n\ttext".encode()

        written = upload_memory_files_to_share(self.tenant_id, {self.first_path: "café\x00\n\t\x01text"})

        self.assertEqual(written, 1)
        self.assertEqual(
            self.operations.mock_calls,
            [
                call.check_1(),
                call.upload_1(payload, length=len(payload)),
                call.create_1(),
                call.create_2(),
                call.create_3(),
                call.upload_1(payload, length=len(payload)),
            ],
        )
        self.assertEqual(
            self.dir_cls.call_args_list,
            [
                call(
                    account_url="https://storage.file.core.windows.net",
                    share_name="ws-tenant",
                    directory_path=path,
                    credential="dummy-key",
                )
                for path in self.parents
            ],
        )

    def test_new_files_create_shared_parents_only_once(self):
        nested_path = "memory/journal/daily/nested/second.md"
        self.files[nested_path] = self.files.pop(self.second_path)
        self.clients[nested_path] = self.clients.pop(self.second_path)
        nested = self.directories["memory/journal/daily/nested"] = MagicMock()
        self.operations.attach_mock(nested.create_directory, "create_4")
        for client in self.clients.values():
            client.upload_file.side_effect = [self._missing_parent(), None]

        self.assertEqual(upload_memory_files_to_share(self.tenant_id, self.files), 2)

        self.assertEqual(
            self.operations.mock_calls,
            [
                call.check_1(),
                call.upload_1(b"first", length=5),
                call.create_1(),
                call.create_2(),
                call.create_3(),
                call.upload_1(b"first", length=5),
                call.check_2(),
                call.upload_2(b"second", length=6),
                call.create_4(),
                call.upload_2(b"second", length=6),
            ],
        )
        for directory in self.directories.values():
            directory.create_directory.assert_called_once_with()

    def test_directory_already_exists_stays_silent(self):
        for client in self.clients.values():
            client.upload_file.side_effect = [self._missing_parent(), None]
        for directory in self.directories.values():
            directory.create_directory.side_effect = _storage_error(
                ResourceExistsError, StorageErrorCode.RESOURCE_ALREADY_EXISTS
            )

        with self.assertNoLogs("apps.orchestrator.memory_sync", level="WARNING"):
            written = upload_memory_files_to_share(self.tenant_id, self.files)

        self.assertEqual(written, 2)
        for directory in self.directories.values():
            directory.create_directory.assert_called_once_with()
        for path, client in self.clients.items():
            payload = self.files[path].encode()
            self.assertEqual(client.upload_file.call_args_list, [call(payload, length=len(payload))] * 2)

    def test_other_directory_conflicts_warn_and_remain_best_effort(self):
        for code in ("ShareBeingDeleted", None):
            with self.subTest(code=code):
                for client in self.clients.values():
                    client.reset_mock()
                    client.upload_file.side_effect = [self._missing_parent(), None]
                directory = self.directories[self.parents[0]]
                directory.reset_mock()
                directory.create_directory.side_effect = [_storage_error(ResourceExistsError, code), None]

                with self.assertLogs("apps.orchestrator.memory_sync", level="WARNING") as captured:
                    written = upload_memory_files_to_share(self.tenant_id, self.files)

                self._assert_warning(captured, "directory conflict", "ResourceExistsError", code)
                self.assertEqual(written, 1)
                self.clients[self.first_path].upload_file.assert_called_once_with(b"first", length=5)
                self.assertEqual(self.clients[self.second_path].upload_file.call_count, 2)
                # The failed create must not cache the parent for the next file.
                self.assertEqual(directory.create_directory.call_count, 2)

    def test_share_not_found_upload_warns_and_batch_continues(self):
        self.clients[self.first_path].upload_file.side_effect = _storage_error(ResourceNotFoundError, "ShareNotFound")

        with self.assertLogs("apps.orchestrator.memory_sync", level="WARNING") as captured:
            written = upload_memory_files_to_share(self.tenant_id, self.files)

        self._assert_warning(captured, "upload failed", "ResourceNotFoundError", "ShareNotFound")
        self.assertEqual(written, 1)
        self.clients[self.first_path].upload_file.assert_called_once_with(b"first", length=5)
        self.clients[self.second_path].upload_file.assert_called_once_with(b"second", length=6)
        self._assert_no_creates()

    def test_retry_failure_warns_without_further_attempts_and_batch_continues(self):
        error = self._missing_parent()
        self.clients[self.first_path].upload_file.side_effect = [self._missing_parent(), error]

        with self.assertLogs("apps.orchestrator.memory_sync", level="WARNING") as captured:
            written = upload_memory_files_to_share(self.tenant_id, self.files)

        self._assert_warning(captured, "upload failed", "ResourceNotFoundError", error.error_code)
        self.assertEqual(written, 1)
        self.assertEqual(self.clients[self.first_path].upload_file.call_count, 2)
        self.clients[self.second_path].upload_file.assert_called_once_with(b"second", length=6)
        for directory in self.directories.values():
            directory.create_directory.assert_called_once_with()

    def test_unexpected_upload_exception_propagates(self):
        error = RuntimeError("private response body")
        self.clients[self.first_path].upload_file.side_effect = error

        with self.assertRaises(RuntimeError) as caught:
            upload_memory_files_to_share(self.tenant_id, self.files)

        self.assertIs(caught.exception, error)
        self.clients[self.first_path].upload_file.assert_called_once_with(b"first", length=5)
        self.clients[self.second_path].get_file_properties.assert_not_called()
        self._assert_no_creates()


class RenderMemoryFilesTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Sync", telegram_chat_id=808080)

    def test_empty_when_no_documents(self):
        # Tenant creation seeds starter docs; clear them to test empty state.
        Document.objects.filter(tenant=self.tenant).delete()
        files = render_memory_files(self.tenant)
        self.assertEqual(files, {})

    def test_renders_non_daily_documents(self):
        # Clear seeded docs to test with controlled data only.
        Document.objects.filter(tenant=self.tenant).delete()
        Document.objects.create(
            tenant=self.tenant,
            kind="memory",
            slug="long-term",
            title="Long-Term Memory",
            markdown="Important stuff",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind="goal",
            slug="fitness",
            title="Fitness Goal",
            markdown="Run a marathon",
        )

        files = render_memory_files(self.tenant)

        self.assertEqual(len(files), 2)
        self.assertIn("memory/journal/memory/long-term.md", files)
        self.assertIn("memory/journal/goal/fitness.md", files)
        self.assertIn("# Long-Term Memory", files["memory/journal/memory/long-term.md"])
        self.assertIn("Important stuff", files["memory/journal/memory/long-term.md"])

    def test_reply_artifact_projects_to_project_path(self):
        from apps.journal.reply_artifacts import upsert_reply_artifact

        Document.objects.filter(tenant=self.tenant).delete()
        doc = upsert_reply_artifact(
            tenant=self.tenant,
            source="proactive",
            dedup_key="memory-sync-artifact",
            title="Table from chat",
            markdown="| A |\n| --- |\n| value\x01 |",
        )

        files = render_memory_files(self.tenant)
        path = f"memory/journal/project/{doc.slug}.md"
        self.assertIn(path, files)
        self.assertIn("| value", files[path])

    @override_settings(AZURE_STORAGE_ACCOUNT_NAME="storage", AZURE_RESOURCE_GROUP="rg")
    @patch("azure.storage.fileshare.ShareFileClient")
    @patch("azure.storage.fileshare.ShareDirectoryClient")
    @patch("azure.storage.fileshare.ShareClient")
    @patch("apps.orchestrator.azure_client.get_storage_client")
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    def test_reply_artifact_share_upload_sanitizes_controls(
        self,
        _mock_mode,
        get_storage_client,
        _share_client_cls,
        _directory_client_cls,
        file_client_cls,
    ):
        from azure.core.exceptions import ResourceNotFoundError

        keys = MagicMock()
        keys.keys = [MagicMock(value="secret")]
        get_storage_client.return_value.storage_accounts.list_keys.return_value = keys
        file_client = file_client_cls.return_value
        file_client.get_file_properties.side_effect = ResourceNotFoundError("missing")

        written = upload_memory_files_to_share(
            str(self.tenant.id),
            {"memory/journal/project/artifact.md": "title\n\t| value\x00\x01 |"},
        )

        self.assertEqual(written, 1)
        uploaded = file_client.upload_file.call_args.args[0]
        self.assertEqual(uploaded, b"title\n\t| value |")

    def test_includes_recent_dailies_excludes_old(self):
        today = timezone.now().date()
        old_date = today - timedelta(days=60)

        Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug=str(today),
            title=f"Daily {today}",
            markdown="Today's note",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug=str(old_date),
            title=f"Daily {old_date}",
            markdown="Old note",
        )

        files = render_memory_files(self.tenant)

        self.assertIn(f"memory/journal/daily/{today}.md", files)
        self.assertNotIn(f"memory/journal/daily/{old_date}.md", files)

    def test_excludes_other_tenants(self):
        # Clear seeded docs to test isolation with controlled data only.
        Document.objects.filter(tenant=self.tenant).delete()
        other = create_tenant(display_name="Other", telegram_chat_id=909090)
        Document.objects.filter(tenant=other).delete()
        Document.objects.create(
            tenant=other,
            kind="memory",
            slug="secret",
            title="Secret",
            markdown="Not yours",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind="memory",
            slug="mine",
            title="Mine",
            markdown="My stuff",
        )

        files = render_memory_files(self.tenant)

        self.assertEqual(len(files), 1)
        self.assertIn("memory/journal/memory/mine.md", files)

    def test_skips_docs_with_ntfs_hostile_path_components(self):
        """Defense-in-depth: even if a doc with a path-hostile kind/slug
        somehow lands in the DB (direct write, pre-validation legacy row),
        render_memory_files must skip it rather than build an NTFS-hostile
        path that grinds the SMB sync.

        Regression: a ``kind=':' slug=':'`` row on the canary tenant produced
        ``memory/journal/:/:.md`` and made ~6 failed SMB roundtrips per
        sync invocation. See migration 0017.
        """
        Document.objects.filter(tenant=self.tenant).delete()
        # Bypass field-choice validation via direct bulk_create (the model
        # has `choices=` but Django doesn't enforce it at the DB layer).
        Document.objects.bulk_create(
            [
                Document(
                    tenant=self.tenant,
                    kind=":",
                    slug=":",
                    title="garbage",
                    markdown="",
                ),
                Document(
                    tenant=self.tenant,
                    kind="cron",
                    slug="_sync:Heartbeat Check-in",
                    title="misrouted sync",
                    markdown="content",
                ),
                Document(
                    tenant=self.tenant,
                    kind="memory",
                    slug="valid-slug",
                    title="ok",
                    markdown="kept",
                ),
            ]
        )

        files = render_memory_files(self.tenant)

        # Only the valid row produces a file.
        self.assertEqual(len(files), 1)
        self.assertIn("memory/journal/memory/valid-slug.md", files)
        # Hostile paths are NOT in the output.
        self.assertNotIn("memory/journal/:/:.md", files)
        self.assertNotIn(
            "memory/journal/cron/_sync:Heartbeat Check-in.md",
            files,
        )
