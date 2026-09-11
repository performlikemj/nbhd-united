"""Upload-first parent creation and bounded retry regression tests."""

from unittest.mock import MagicMock, call, patch

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.storage.fileshare import StorageErrorCode
from django.test import SimpleTestCase, override_settings

from apps.orchestrator.azure_client import _put_share_file


def _storage_error(error_type, code):
    error = error_type("storage error")
    # process_storage_error assigns this attribute after constructing the exception.
    error.error_code = code
    return error


@override_settings(AZURE_STORAGE_ACCOUNT_NAME="storage", AZURE_RESOURCE_GROUP="rg")
class PutShareFileParentsTests(SimpleTestCase):
    path = "workspace/memory/journal/note.md"
    parents = ("workspace", "workspace/memory", "workspace/memory/journal")
    payload = b"\x00raw data"

    def setUp(self):
        self.enterContext(patch("apps.orchestrator.azure_client._is_mock", return_value=False))
        storage = self.enterContext(patch("apps.orchestrator.azure_client.get_storage_client"))
        keys = MagicMock()
        keys.keys = [MagicMock(value="dummy-key")]
        storage.return_value.storage_accounts.list_keys.return_value = keys
        self.file_cls = self.enterContext(patch("azure.storage.fileshare.ShareFileClient"))
        self.dir_cls = self.enterContext(patch("azure.storage.fileshare.ShareDirectoryClient"))
        self.upload = self.file_cls.return_value.upload_file
        self.directories = {path: MagicMock() for path in self.parents}
        self.dir_cls.side_effect = lambda **kwargs: self.directories[kwargs["directory_path"]]
        self.operations = MagicMock()
        self.operations.attach_mock(self.upload, "upload")
        for depth, directory in enumerate(self.directories.values(), start=1):
            self.operations.attach_mock(directory.create_directory, f"create_{depth}")

    def _missing_parent(self):
        return _storage_error(ResourceNotFoundError, StorageErrorCode.PARENT_NOT_FOUND)

    def _assert_no_creates(self):
        self.dir_cls.assert_not_called()
        for directory in self.directories.values():
            directory.create_directory.assert_not_called()

    def test_existing_parents_upload_without_directory_calls(self):
        _put_share_file("tenant", self.path, data=self.payload)

        self.upload.assert_called_once_with(self.payload, length=len(self.payload))
        self._assert_no_creates()

    def test_missing_parents_created_in_order_before_one_retry(self):
        self.upload.side_effect = [self._missing_parent(), None]

        _put_share_file("tenant", self.path, data=self.payload)

        self.assertEqual(
            self.operations.mock_calls,
            [
                call.upload(self.payload, length=len(self.payload)),
                call.create_1(),
                call.create_2(),
                call.create_3(),
                call.upload(self.payload, length=len(self.payload)),
            ],
        )
        self.assertEqual([entry.kwargs["directory_path"] for entry in self.dir_cls.call_args_list], list(self.parents))

    def test_concurrent_parent_creation_is_tolerated(self):
        self.upload.side_effect = [self._missing_parent(), None]
        self.directories[self.parents[0]].create_directory.side_effect = _storage_error(
            ResourceExistsError, StorageErrorCode.RESOURCE_ALREADY_EXISTS
        )

        _put_share_file("tenant", self.path, data=self.payload)

        self.assertEqual(self.upload.call_count, 2)
        for directory in self.directories.values():
            directory.create_directory.assert_called_once_with()

    def test_other_parent_creation_errors_propagate_without_retry(self):
        for error in (
            _storage_error(ResourceExistsError, "ShareBeingDeleted"),
            _storage_error(ResourceExistsError, None),
            _storage_error(HttpResponseError, "AuthorizationFailure"),
            _storage_error(ResourceNotFoundError, StorageErrorCode.PARENT_NOT_FOUND),
            RuntimeError("unexpected failure"),
        ):
            with self.subTest(error=type(error).__name__, code=getattr(error, "error_code", None)):
                self.upload.reset_mock()
                self.upload.side_effect = [self._missing_parent(), None]
                self.dir_cls.reset_mock()
                self.directories[self.parents[0]].create_directory.side_effect = error

                with self.assertRaises(type(error)) as caught:
                    _put_share_file("tenant", self.path, data=self.payload)

                self.assertIs(caught.exception, error)
                self.upload.assert_called_once_with(self.payload, length=len(self.payload))
                self.assertEqual(self.dir_cls.call_count, 1)

    def test_other_not_found_codes_propagate_without_creates(self):
        for code in ("ShareNotFound", None):
            with self.subTest(code=code):
                error = _storage_error(ResourceNotFoundError, code)
                self.upload.reset_mock()
                self.upload.side_effect = error

                with self.assertRaises(ResourceNotFoundError) as caught:
                    _put_share_file("tenant", self.path, data=self.payload)

                self.assertIs(caught.exception, error)
                self.upload.assert_called_once_with(self.payload, length=len(self.payload))
                self._assert_no_creates()

    def test_ensure_dirs_false_propagates_missing_parent(self):
        error = self._missing_parent()
        self.upload.side_effect = error

        with self.assertRaises(ResourceNotFoundError) as caught:
            _put_share_file("tenant", self.path, data=self.payload, ensure_dirs=False)

        self.assertIs(caught.exception, error)
        self.upload.assert_called_once_with(self.payload, length=len(self.payload))
        self._assert_no_creates()

    def test_retry_failure_propagates_without_further_attempts(self):
        error = self._missing_parent()
        self.upload.side_effect = [self._missing_parent(), error]

        with self.assertRaises(ResourceNotFoundError) as caught:
            _put_share_file("tenant", self.path, data=self.payload)

        self.assertIs(caught.exception, error)
        self.assertEqual(self.upload.call_count, 2)
        for directory in self.directories.values():
            directory.create_directory.assert_called_once_with()

    def test_text_sanitized_on_both_attempts(self):
        self.upload.side_effect = [self._missing_parent(), None]
        sanitized = "café\n\ttext".encode()

        _put_share_file("tenant", self.path, text="café\x00\n\t\x01text")

        self.assertEqual(self.upload.call_args_list, [call(sanitized, length=len(sanitized))] * 2)

    def test_skip_if_exists_returns_without_upload_or_creates(self):
        _put_share_file("tenant", self.path, data=self.payload, skip_if_exists=True)

        self.file_cls.return_value.get_file_properties.assert_called_once_with()
        self.upload.assert_not_called()
        self._assert_no_creates()
