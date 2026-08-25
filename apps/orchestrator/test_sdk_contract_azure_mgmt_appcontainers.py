"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from azure.core.credentials import AccessToken
from azure.core.exceptions import ResourceExistsError
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers.models import (
    AzureFileProperties,
    ContainerAppProbe,
    ContainerAppProbeHttpGet,
    ManagedEnvironmentStorage,
    ManagedEnvironmentStorageProperties,
    Volume,
    VolumeMount,
)
from django.test import SimpleTestCase


class _DummyCredential:
    def get_token(self, *_scopes, **_kwargs):
        return AccessToken("offline-contract-test", 4_102_444_800)


class AzureContainerAppsSdkContractTest(SimpleTestCase):
    def setUp(self):
        self.client = ContainerAppsAPIClient(_DummyCredential(), "subscription-id")

    def test_container_app_operation_signatures_accept_our_calls(self):
        operations = self.client.container_apps

        inspect.signature(operations.begin_create_or_update).bind("resource-group", "app", {})
        inspect.signature(operations.get).bind("resource-group", "app")
        inspect.signature(operations.list_by_resource_group).bind("resource-group")
        inspect.signature(operations.begin_delete).bind("resource-group", "app")

    def test_revision_and_storage_operation_signatures_accept_our_calls(self):
        revisions = self.client.container_apps_revisions
        storages = self.client.managed_environments_storages

        inspect.signature(revisions.list_revisions).bind("resource-group", "app")
        inspect.signature(revisions.activate_revision).bind("resource-group", "app", "revision")
        inspect.signature(revisions.deactivate_revision).bind("resource-group", "app", "revision")
        inspect.signature(storages.create_or_update).bind(
            resource_group_name="resource-group",
            environment_name="environment",
            storage_name="storage",
            storage_envelope=ManagedEnvironmentStorage(),
        )
        inspect.signature(storages.delete).bind(
            resource_group_name="resource-group",
            environment_name="environment",
            storage_name="storage",
        )

    def test_storage_models_accept_our_constructor_kwargs(self):
        model = ManagedEnvironmentStorage(
            properties=ManagedEnvironmentStorageProperties(
                azure_file=AzureFileProperties(
                    account_name="account",
                    account_key="key",
                    access_mode="ReadWrite",
                    share_name="share",
                )
            )
        )

        self.assertEqual(model.properties.azure_file.share_name, "share")
        self.assertEqual(model.properties.azure_file.access_mode, "ReadWrite")

    def test_volume_and_probe_models_accept_our_constructor_kwargs(self):
        volume = Volume(name="cache", storage_type="EmptyDir")
        mount = VolumeMount(volume_name="cache", mount_path="/cache")
        probe = ContainerAppProbe(
            type="Readiness",
            http_get=ContainerAppProbeHttpGet(path="/ready", port=18790, scheme="HTTP"),
            initial_delay_seconds=3,
            period_seconds=5,
            timeout_seconds=2,
            failure_threshold=3,
            success_threshold=1,
        )

        self.assertEqual((volume.name, volume.storage_type), ("cache", "EmptyDir"))
        self.assertEqual((mount.volume_name, mount.mount_path), ("cache", "/cache"))
        self.assertEqual(probe.http_get.path, "/ready")
        self.assertEqual(probe.type, "Readiness")

    def test_caught_revision_exception_still_exists(self):
        self.assertTrue(issubclass(ResourceExistsError, Exception))
