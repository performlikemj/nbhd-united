"""Tests for the shared OpenRouter chat-completion wrapper with fallback."""

from unittest.mock import patch

import requests
from django.test import TestCase, override_settings  # noqa: F401

from apps.billing.constants import DEEPSEEK_MODEL, MINIMAX_MODEL
from apps.billing.models import ModelHealth
from apps.common.openrouter import chat_completion, normalize_model_id


class _Resp:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


_OK = {"choices": [{"message": {"content": "hello"}}], "usage": {"total_tokens": 3}}
_MSGS = [{"role": "user", "content": "hi"}]


class NormalizeModelIdTest(TestCase):
    def test_strips_openrouter_prefix(self):
        self.assertEqual(normalize_model_id("openrouter/deepseek/deepseek-v4-pro"), "deepseek/deepseek-v4-pro")

    def test_leaves_other_prefixes(self):
        self.assertEqual(normalize_model_id("anthropic/claude-haiku-4-5"), "anthropic/claude-haiku-4-5")


@override_settings(OPENROUTER_API_KEY="sk-test")
class ChatCompletionFallbackTest(TestCase):
    @patch("apps.common.openrouter.requests.post")
    def test_first_model_success(self, mock_post):
        mock_post.return_value = _Resp(_OK)
        data, used = chat_completion([DEEPSEEK_MODEL, MINIMAX_MODEL], _MSGS)
        self.assertEqual(used, DEEPSEEK_MODEL)
        self.assertEqual(data["choices"][0]["message"]["content"], "hello")
        # bare slug sent to the API, not the openrouter/ form
        self.assertEqual(mock_post.call_args.kwargs["json"]["model"], "deepseek/deepseek-v4-pro")
        self.assertEqual(mock_post.call_count, 1)
        self.assertTrue(ModelHealth.objects.get(model_id=DEEPSEEK_MODEL).is_reachable)

    @patch("apps.common.openrouter.requests.post")
    def test_falls_back_to_second_on_failure(self, mock_post):
        mock_post.side_effect = [requests.exceptions.ConnectionError("boom"), _Resp(_OK)]
        data, used = chat_completion([DEEPSEEK_MODEL, MINIMAX_MODEL], _MSGS)
        self.assertEqual(used, MINIMAX_MODEL)
        self.assertEqual(mock_post.call_count, 2)
        self.assertGreaterEqual(ModelHealth.objects.get(model_id=DEEPSEEK_MODEL).consecutive_failures, 1)
        self.assertFalse(ModelHealth.objects.get(model_id=DEEPSEEK_MODEL).is_reachable)
        self.assertTrue(ModelHealth.objects.get(model_id=MINIMAX_MODEL).is_reachable)

    @patch("apps.common.openrouter.requests.post")
    def test_200_with_error_body_is_treated_as_failure(self, mock_post):
        mock_post.side_effect = [_Resp({"error": {"message": "rate limited"}}), _Resp(_OK)]
        _data, used = chat_completion([DEEPSEEK_MODEL, MINIMAX_MODEL], _MSGS)
        self.assertEqual(used, MINIMAX_MODEL)

    @patch("apps.common.openrouter.requests.post")
    def test_raises_last_error_when_all_fail(self, mock_post):
        mock_post.side_effect = [
            requests.exceptions.ConnectionError("a"),
            requests.exceptions.Timeout("b"),
        ]
        with self.assertRaises(requests.exceptions.Timeout):
            chat_completion([DEEPSEEK_MODEL, MINIMAX_MODEL], _MSGS)

    @patch("apps.common.openrouter.requests.post")
    def test_accepts_single_model_string(self, mock_post):
        mock_post.return_value = _Resp(_OK)
        _data, used = chat_completion(DEEPSEEK_MODEL, _MSGS)
        self.assertEqual(used, DEEPSEEK_MODEL)

    def test_missing_api_key_raises(self):
        with self.settings(OPENROUTER_API_KEY=""), self.assertRaises(RuntimeError):
            chat_completion([DEEPSEEK_MODEL], _MSGS, api_key="")
