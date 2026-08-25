"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.fileshare import ShareClient, ShareDirectoryClient, ShareFileClient
from azure.storage.fileshare._download import StorageStreamDownloader
from django.test import SimpleTestCase


class AzureFileShareSdkContractTest(SimpleTestCase):
    def test_client_constructors_accept_our_keyword_shapes(self):
        common = {
            "account_url": "https://example.file.core.windows.net",
            "share_name": "share",
            "credential": "dummy-key",
        }

        ShareClient(**common)
        ShareDirectoryClient(**common, directory_path="workspace/memory")
        ShareFileClient(**common, file_path="workspace/MEMORY.md")

    def test_file_methods_accept_our_calls_and_downloaders_have_readall(self):
        client = ShareFileClient(
            account_url="https://example.file.core.windows.net",
            share_name="share",
            file_path="workspace/MEMORY.md",
            credential="dummy-key",
        )

        inspect.signature(client.upload_file).bind(b"data", length=4)
        inspect.signature(client.download_file).bind()
        inspect.signature(client.get_file_properties).bind()
        inspect.signature(client.delete_file).bind()
        self.assertTrue(callable(StorageStreamDownloader.readall))

    def test_directory_and_share_methods_accept_our_calls(self):
        share = ShareClient(
            account_url="https://example.file.core.windows.net",
            share_name="share",
            credential="dummy-key",
        )
        directory = ShareDirectoryClient(
            account_url="https://example.file.core.windows.net",
            share_name="share",
            directory_path="workspace",
            credential="dummy-key",
        )

        inspect.signature(share.get_share_properties).bind()
        inspect.signature(directory.create_directory).bind()
        inspect.signature(directory.list_directories_and_files).bind()
        inspect.signature(directory.get_file_client).bind("MEMORY.md")

    def test_caught_exceptions_still_exist(self):
        self.assertTrue(issubclass(ResourceNotFoundError, Exception))
        self.assertTrue(issubclass(ResourceExistsError, Exception))
