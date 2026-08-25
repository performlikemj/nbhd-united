"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

import httpx
from django.test import SimpleTestCase


class HttpxSdkContractTest(SimpleTestCase):
    def test_module_request_functions_accept_our_common_kwargs(self):
        for method in (httpx.get, httpx.delete):
            inspect.signature(method).bind(
                "https://example.test/resource",
                headers={"Authorization": "Bearer offline"},
                timeout=5,
            )
        for method in (httpx.post, httpx.put, httpx.patch):
            inspect.signature(method).bind(
                "https://example.test/resource",
                json={"value": 1},
                headers={"Authorization": "Bearer offline"},
                timeout=5,
            )

    def test_client_and_timeout_constructors_accept_our_kwargs(self):
        timeout = httpx.Timeout(connect=1.0, read=2.0, write=1.0, pool=0.25)
        client = httpx.Client(http2=True, base_url="https://example.test", timeout=timeout)

        inspect.signature(client.get).bind("/resource", params={"page": 1})
        inspect.signature(client.post).bind("/resource", json={"value": 1})
        client.close()
        inspect.signature(httpx.AsyncClient).bind(timeout=timeout)

    def test_real_response_keeps_methods_and_attributes_we_read(self):
        response = httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("GET", "https://example.test/resource"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertTrue(response.content)
        self.assertIs(response.raise_for_status(), response)

    def test_caught_exception_paths_exist(self):
        for exception in (
            httpx.HTTPError,
            httpx.RequestError,
            httpx.TimeoutException,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.ConnectError,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.HTTPStatusError,
        ):
            self.assertTrue(issubclass(exception, httpx.HTTPError))
