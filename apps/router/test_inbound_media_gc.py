"""GC coverage for inbound media (images AND PDFs).

``cleanup_inbound_media_task`` sweeps ``workspace/media/inbound/`` directory-wide
with NO extension filter, so an app-uploaded PDF (``doc_<hash>.pdf``) is aged out
at 24h exactly like a photo (``photo_<hash>.jpg``). This pins that behavior so a
future "only delete images" narrowing can't silently leak PDFs on the share.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

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
