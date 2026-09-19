from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .gateway_url import gateway_base_url


class GatewayURLTests(SimpleTestCase):
    def test_http_requires_every_guard(self):
        for debug in (False, True):
            for mock in ("false", "true"):
                for synthetic in (False, True):
                    tenant = SimpleNamespace(container_fqdn="127.0.0.1:19443", is_synthetic=synthetic)
                    with (
                        self.subTest(debug=debug, mock=mock, synthetic=synthetic),
                        override_settings(DEBUG=debug),
                        patch.dict("os.environ", AZURE_MOCK=mock),
                    ):
                        scheme = "http" if debug and mock == "true" and synthetic else "https"
                        self.assertEqual(gateway_base_url(tenant), f"{scheme}://127.0.0.1:19443")

    @override_settings(DEBUG=True)
    @patch.dict("os.environ", AZURE_MOCK="true")
    def test_only_exact_loopback_with_valid_port(self):
        for host in (
            "localhost:19443",
            "[::1]:19443",
            "127.0.0.2:19443",
            "127.0.0.1:0",
            "127.0.0.1:65536",
            "127.0.0.1:19443/path",
            "127.0.0.1:19443@evil.test",
            "gateway.azurecontainerapps.io",
            "http://127.0.0.1:19443",
        ):
            with self.subTest(host=host):
                self.assertEqual(
                    gateway_base_url(SimpleNamespace(container_fqdn=host, is_synthetic=True)), f"https://{host}"
                )
        self.assertEqual(gateway_base_url(fqdn="127.0.0.1:19443"), "https://127.0.0.1:19443")
