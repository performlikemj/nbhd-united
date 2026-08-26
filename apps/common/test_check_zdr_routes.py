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
            return _response({"data": {"endpoints": [{"provider_name": "Azure"}, {"provider_name": "OpenAI"}]}})
        raise AssertionError(f"Unexpected URL: {url}")

    @patch("apps.common.management.commands.check_zdr_routes.requests.get")
    def test_mixed_embedding_endpoints_pass_when_one_is_zdr(self, get):
        get.side_effect = self._success_response
        stdout = StringIO()

        call_command("check_zdr_routes", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn(
            f"{_STT_MODEL} rule=all_endpoints_zdr zdr=[DeepInfra, Groq] non_zdr=[none]",
            output,
        )
        self.assertIn(
            f"{_EMBEDDING_MODEL} rule=at_least_one_endpoint_zdr zdr=[Azure] non_zdr=[OpenAI]",
            output,
        )
        self.assertIn("ZDR route rules verified for 2 model(s).", output)
        self.assertEqual(get.call_count, 3)
        for call in get.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 30)
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer route-check-test-key")

    @patch("apps.common.management.commands.check_zdr_routes.requests.get")
    def test_embedding_with_zero_zdr_endpoints_fails(self, get):
        def responses(url, **kwargs):
            response = self._success_response(url, **kwargs)
            if _EMBEDDING_MODEL in url:
                return _response({"data": {"endpoints": [{"provider_name": "OpenAI"}]}})
            return response

        get.side_effect = responses
        stdout = StringIO()

        with self.assertRaises(CommandError) as raised:
            call_command("check_zdr_routes", stdout=stdout)

        self.assertIn(
            f"{_EMBEDDING_MODEL} rule=at_least_one_endpoint_zdr zdr=[none] non_zdr=[OpenAI]",
            stdout.getvalue(),
        )
        self.assertIn("has no ZDR embedding endpoint", str(raised.exception))

    @patch("apps.common.management.commands.check_zdr_routes.requests.get")
    def test_stt_with_any_non_zdr_endpoint_fails(self, get):
        def responses(url, **kwargs):
            response = self._success_response(url, **kwargs)
            if _STT_MODEL in url and not url.endswith("/endpoints/zdr"):
                return _response(
                    {"data": {"endpoints": [{"provider_name": "DeepInfra"}, {"provider_name": "OtherCloud"}]}}
                )
            return response

        get.side_effect = responses
        stdout = StringIO()

        with self.assertRaises(CommandError) as raised:
            call_command("check_zdr_routes", stdout=stdout)

        self.assertIn(
            f"{_STT_MODEL} rule=all_endpoints_zdr zdr=[DeepInfra] non_zdr=[OtherCloud]",
            stdout.getvalue(),
        )
        self.assertIn("has non-ZDR STT endpoints", str(raised.exception))

    @patch(
        "apps.common.management.commands.check_zdr_routes.requests.get",
        side_effect=requests.ConnectionError("route-check-test-key must stay secret"),
    )
    def test_network_error_fails_without_printing_secret(self, _get):
        with self.assertRaises(CommandError) as raised:
            call_command("check_zdr_routes")

        self.assertNotIn("route-check-test-key", str(raised.exception))
