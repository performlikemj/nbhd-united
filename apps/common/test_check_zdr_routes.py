from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

_STT_MODEL = "openai/whisper-large-v3-turbo"
_EMBEDDING_MODEL = "openai/text-embedding-3-small"


def _response(payload):
    response = MagicMock()
    response.json.return_value = payload
    return response


@override_settings(OPENROUTER_API_KEY="route-check-test-key", OPENROUTER_STT_MODEL=_STT_MODEL)
class CheckZdrRoutesCommandTests(SimpleTestCase):
    @staticmethod
    def _success_response(url, **kwargs):
        del kwargs
        if url.endswith("/endpoints/zdr"):
            return _response(
                {
                    "data": [
                        {"model_id": _STT_MODEL, "provider_name": "DeepInfra"},
                        {"model_id": _STT_MODEL, "provider_name": "Groq"},
                        {"model_id": _EMBEDDING_MODEL, "provider_name": "Azure"},
                    ]
                }
            )
        if _STT_MODEL in url:
            return _response({"data": {"endpoints": [{"provider_name": "DeepInfra"}, {"provider_name": "Groq"}]}})
        if _EMBEDDING_MODEL in url:
            return _response({"data": {"endpoints": [{"provider_name": "Azure"}]}})
        raise AssertionError(f"Unexpected URL: {url}")

    @patch("apps.common.management.commands.check_zdr_routes.requests.get")
    def test_all_advertised_endpoints_are_zdr(self, get):
        get.side_effect = self._success_response
        stdout = StringIO()

        call_command("check_zdr_routes", stdout=stdout)

        self.assertIn("ZDR routes verified for 2 model(s).", stdout.getvalue())
        self.assertEqual(get.call_count, 3)
        for call in get.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 30)
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer route-check-test-key")

    @patch("apps.common.management.commands.check_zdr_routes.requests.get")
    def test_non_zdr_provider_names_are_reported_and_command_fails(self, get):
        def responses(url, **kwargs):
            response = self._success_response(url, **kwargs)
            if _STT_MODEL in url and not url.endswith("/endpoints/zdr"):
                return _response(
                    {"data": {"endpoints": [{"provider_name": "DeepInfra"}, {"provider_name": "OtherCloud"}]}}
                )
            if _EMBEDDING_MODEL in url:
                return _response({"data": {"endpoints": [{"provider_name": "Azure"}, {"provider_name": "OtherEmbed"}]}})
            return response

        get.side_effect = responses
        stderr = StringIO()

        with self.assertRaises(CommandError):
            call_command("check_zdr_routes", stderr=stderr)

        self.assertIn(f"{_STT_MODEL} -> OtherCloud", stderr.getvalue())
        self.assertIn(f"{_EMBEDDING_MODEL} -> OtherEmbed", stderr.getvalue())
        self.assertNotIn("route-check-test-key", stderr.getvalue())

    @patch(
        "apps.common.management.commands.check_zdr_routes.requests.get",
        side_effect=requests.ConnectionError("route-check-test-key must stay secret"),
    )
    def test_network_error_fails_without_printing_secret(self, _get):
        stderr = StringIO()

        with self.assertRaises(CommandError) as raised:
            call_command("check_zdr_routes", stderr=stderr)

        self.assertNotIn("route-check-test-key", str(raised.exception))
        self.assertNotIn("route-check-test-key", stderr.getvalue())
