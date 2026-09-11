"""GC coverage for inbound media (images AND PDFs).

``cleanup_inbound_media_task`` sweeps ``workspace/media/inbound/`` directory-wide
with NO extension filter, so an app-uploaded PDF (``doc_<hash>.pdf``) is aged out
at 24h exactly like a photo (``photo_<hash>.jpg``). This pins that behavior so a
future "only delete images" narrowing can't silently leak PDFs on the share.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from django.test import TestCase, override_settings

from apps.router.tasks import cleanup_inbound_media_task
from apps.tenants.models import Tenant, User


def _file_item(name: str) -> dict:
    return {"name": name, "is_directory": False}


@override_settings(AZURE_STORAGE_ACCOUNT_NAME="teststorage", AZURE_RESOURCE_GROUP="rg-test")
class InboundMediaGCTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="gc", email="gc@example.com")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_id="oc-gc-1",
        )

    def _mock_storage(self):
        self.enterContext(patch("apps.orchestrator.azure_client._is_mock", return_value=False))
        storage = self.enterContext(patch("apps.orchestrator.azure_client.get_storage_client"))
        keys = MagicMock()
        keys.keys = [MagicMock(value="dummy-key")]
        storage.return_value.storage_accounts.list_keys.return_value = keys
        return self.enterContext(patch("azure.storage.fileshare.ShareDirectoryClient"))

    def test_expected_listing_absence_stays_silent(self):
        directory_cls = self._mock_storage()
        for code in ("ResourceNotFound", "ParentNotFound"):
            with self.subTest(code=code):
                error = ResourceNotFoundError("private response body")
                error.error_code = code
                directory_cls.return_value.list_directories_and_files.side_effect = error

                with self.assertNoLogs("apps.router.tasks", level="WARNING"):
                    cleanup_inbound_media_task()

    def test_expected_file_absence_stays_silent(self):
        directory = self._mock_storage().return_value
        directory.list_directories_and_files.return_value = [_file_item("private.pdf")]
        file_client = directory.get_file_client.return_value
        for method in ("get_file_properties", "delete_file"):
            for code in ("ResourceNotFound", "ParentNotFound"):
                with self.subTest(method=method, code=code):
                    file_client.get_file_properties.side_effect = None
                    file_client.delete_file.side_effect = None
                    file_client.get_file_properties.return_value.last_modified = datetime.now(UTC) - timedelta(hours=48)
                    error = ResourceNotFoundError("private response body")
                    error.error_code = code
                    getattr(file_client, method).side_effect = error

                    with self.assertNoLogs("apps.router.tasks", level="WARNING"):
                        cleanup_inbound_media_task()

    def test_unexpected_listing_error_warns_and_continues(self):
        other_user = User.objects.create_user(username="gc-other", email="gc-other@example.com")
        Tenant.objects.create(user=other_user, status=Tenant.Status.ACTIVE, container_id="oc-gc-2")
        directory_cls = self._mock_storage()
        failed, healthy = MagicMock(), MagicMock()
        directory_cls.side_effect = [failed, healthy]
        error = HttpResponseError("private response body")
        error.error_code = "AuthorizationFailure"
        failed.list_directories_and_files.side_effect = error
        healthy.list_directories_and_files.return_value = [_file_item("private.pdf")]
        file_client = healthy.get_file_client.return_value
        file_client.get_file_properties.return_value.last_modified = datetime.now(UTC) - timedelta(hours=48)

        with self.assertLogs("apps.router.tasks", level="WARNING") as captured:
            cleanup_inbound_media_task()

        self.assertEqual(
            captured.output,
            [
                "WARNING:apps.router.tasks:Media cleanup listing failed: type=HttpResponseError code=AuthorizationFailure"
            ],
        )
        self.assertIsNone(captured.records[0].exc_info)
        self.assertEqual(directory_cls.call_count, 2)
        file_client.delete_file.assert_called_once_with()

    def test_unexpected_file_error_warns_and_continues(self):
        directory = self._mock_storage().return_value
        directory.list_directories_and_files.return_value = [_file_item("private.pdf"), _file_item("next.pdf")]
        for method in ("get_file_properties", "delete_file"):
            for code in ("AuthorizationFailure", None):
                with self.subTest(method=method, code=code):
                    failed, healthy = MagicMock(), MagicMock()
                    directory.get_file_client.side_effect = {"private.pdf": failed, "next.pdf": healthy}.__getitem__
                    for file_client in (failed, healthy):
                        file_client.get_file_properties.return_value.last_modified = datetime.now(UTC) - timedelta(
                            hours=48
                        )
                    error = HttpResponseError("private response body")
                    error.error_code = code
                    getattr(failed, method).side_effect = error

                    with self.assertLogs("apps.router.tasks", level="WARNING") as captured:
                        cleanup_inbound_media_task()

                    self.assertEqual(
                        captured.output,
                        [
                            f"WARNING:apps.router.tasks:Media cleanup check/delete failed: type=HttpResponseError code={code}"
                        ],
                    )
                    self.assertIsNone(captured.records[0].exc_info)
                    healthy.delete_file.assert_called_once_with()

    @patch("azure.storage.fileshare.ShareDirectoryClient")
    @patch("apps.orchestrator.azure_client.get_storage_client")
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    def test_pdf_and_image_both_aged_out(self, _mock_is_mock, mock_get_storage, mock_dir_cls):
        # Storage account key lookup.
        keys = MagicMock()
        keys.keys = [MagicMock(value="fake-key")]
        mock_get_storage.return_value.storage_accounts.list_keys.return_value = keys

        old = datetime.now(UTC) - timedelta(hours=48)  # past the 24h cutoff
        fresh = datetime.now(UTC)  # within the window — must survive

        # last_modified per filename.
        ages = {
            "photo_old.jpg": old,
            "doc_old.pdf": old,
            "doc_fresh.pdf": fresh,
        }
        deleted: list[str] = []

        def _get_file_client(name):
            fc = MagicMock()
            fc.get_file_properties.return_value.last_modified = ages[name]
            fc.delete_file.side_effect = lambda: deleted.append(name)
            return fc

        dir_client = mock_dir_cls.return_value
        dir_client.list_directories_and_files.return_value = [_file_item(n) for n in ages]
        dir_client.get_file_client.side_effect = _get_file_client

        cleanup_inbound_media_task()

        # Both the old image AND the old PDF are deleted; the fresh PDF survives.
        self.assertIn("photo_old.jpg", deleted)
        self.assertIn("doc_old.pdf", deleted)
        self.assertNotIn("doc_fresh.pdf", deleted)
