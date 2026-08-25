"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from azure.core.credentials import AccessToken
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.models import RoleAssignmentCreateParameters
from django.test import SimpleTestCase


class _DummyCredential:
    def get_token(self, *_scopes, **_kwargs):
        return AccessToken("offline-contract-test", 4_102_444_800)


class AzureAuthorizationSdkContractTest(SimpleTestCase):
    def test_role_assignment_model_and_create_signature(self):
        parameters = RoleAssignmentCreateParameters(
            role_definition_id="role-definition-id",
            principal_id="principal-id",
            principal_type="ServicePrincipal",
        )
        client = AuthorizationManagementClient(_DummyCredential(), "subscription-id")

        inspect.signature(client.role_assignments.create).bind(
            scope="/subscriptions/subscription-id",
            role_assignment_name="assignment-id",
            parameters=parameters,
        )
        self.assertEqual(parameters.role_definition_id, "role-definition-id")
        self.assertEqual(parameters.principal_type, "ServicePrincipal")
