"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.core.pipeline.transport import HttpRequest, RequestsTransportResponse
from azure.storage.fileshare import ShareClient, ShareDirectoryClient, ShareFileClient, StorageErrorCode
from azure.storage.fileshare._download import StorageStreamDownloader
from azure.storage.fileshare._shared.response_handlers import process_storage_error
from django.test import SimpleTestCase
from requests import Response


class AzureFileShareSdkContractTest(SimpleTestCase):
    def test_authentication_error_code(self):
        self.assertEqual(StorageErrorCode.AUTHENTICATION_FAILED, "AuthenticationFailed")

    def test_parent_creation_error_codes(self):
        self.assertEqual(StorageErrorCode.PARENT_NOT_FOUND, "ParentNotFound")
        self.assertEqual(StorageErrorCode.RESOURCE_ALREADY_EXISTS, "ResourceAlreadyExists")

    def test_storage_error_processing_preserves_header_codes(self):
        for status, code, error_type in (
            (403, "AuthenticationFailed", ClientAuthenticationError),
            (404, "ParentNotFound", ResourceNotFoundError),
            (409, "ResourceAlreadyExists", ResourceExistsError),
        ):
            with self.subTest(status=status, code=code):
                raw_response = Response()
                raw_response.status_code = status
                raw_response.headers["x-ms-error-code"] = code
                raw_response._content = b""
                response = RequestsTransportResponse(
                    HttpRequest("PUT", "https://example.file.core.windows.net/share/dir"), raw_response
                )

                with self.assertRaises(error_type) as caught:
                    process_storage_error(HttpResponseError(message="storage error", response=response))

                self.assertEqual(caught.exception.error_code, code)

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
