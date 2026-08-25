"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

import requests
from django.test import SimpleTestCase


class RequestsSdkContractTest(SimpleTestCase):
    def test_request_functions_accept_our_common_kwargs(self):
        for method in (requests.get, requests.delete):
            inspect.signature(method).bind("https://example.test/resource", headers={"X-Test": "offline"}, timeout=5)
        for method in (requests.post, requests.put, requests.patch):
            inspect.signature(method).bind(
                "https://example.test/resource",
                json={"value": 1},
                headers={"X-Test": "offline"},
                timeout=5,
            )

    def test_caught_exception_hierarchy_exists(self):
        self.assertTrue(issubclass(requests.RequestException, OSError))
        self.assertTrue(issubclass(requests.Timeout, requests.RequestException))
