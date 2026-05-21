"""Coverage for the model-attribution helpers shared by every usage path.

These guard the bug where every usage row was tagged as the ``openclaw``
request-side placeholder instead of the upstream model id, collapsing
"Usage by Model" into a single undifferentiated bucket.
"""

from django.test import SimpleTestCase

from .constants import (
    ANTHROPIC_OPUS_DISPLAY,
    ANTHROPIC_SONNET_DISPLAY,
    ANTHROPIC_SONNET_MODEL,
    KIMI_MODEL,
    MINIMAX_DISPLAY,
    MINIMAX_MODEL,
    display_name_for_model,
)
from .services import extract_model_from_response


class ExtractModelFromResponseTest(SimpleTestCase):
    def test_prefers_usage_model_used_over_top_level_model(self):
        result = {
            "model": "openclaw",
            "usage": {"model_used": MINIMAX_MODEL, "prompt_tokens": 1, "completion_tokens": 1},
        }
        self.assertEqual(extract_model_from_response(result), MINIMAX_MODEL)

    def test_falls_back_to_usage_model_if_model_used_missing(self):
        result = {"model": "openclaw", "usage": {"model": KIMI_MODEL}}
        self.assertEqual(extract_model_from_response(result), KIMI_MODEL)

    def test_falls_back_to_top_level_model_used(self):
        result = {"model_used": "openai/gpt-4o-mini", "usage": {}}
        self.assertEqual(extract_model_from_response(result), "openai/gpt-4o-mini")

    def test_falls_back_to_top_level_model_when_real_provider(self):
        # If OpenClaw ever stops echoing "openclaw" and just sends the real id
        # in the top-level ``model`` field, we should still pick it up.
        result = {"model": "anthropic/claude-sonnet-4.6", "usage": {}}
        self.assertEqual(extract_model_from_response(result), "anthropic/claude-sonnet-4.6")

    def test_rejects_openclaw_placeholder(self):
        # The chat-completions request always sends ``"model": "openclaw"`` —
        # we must not let that round-trip into UsageRecord.model_used.
        result = {"model": "openclaw", "usage": {"model": "openclaw"}}
        self.assertEqual(extract_model_from_response(result), "")

    def test_strips_whitespace(self):
        result = {"usage": {"model_used": "  openrouter/google/gemma-4-31b-it  "}}
        self.assertEqual(extract_model_from_response(result), "openrouter/google/gemma-4-31b-it")

    def test_handles_non_dict_input(self):
        self.assertEqual(extract_model_from_response(None), "")
        self.assertEqual(extract_model_from_response("not-a-dict"), "")
        self.assertEqual(extract_model_from_response([]), "")

    def test_handles_non_string_model_fields(self):
        result = {"model": 42, "usage": {"model_used": None, "model": ["nope"]}}
        self.assertEqual(extract_model_from_response(result), "")


class DisplayNameForModelTest(SimpleTestCase):
    def test_billed_model_uses_mapped_display_name(self):
        self.assertEqual(display_name_for_model(MINIMAX_MODEL), MINIMAX_DISPLAY)

    def test_billed_model_without_openrouter_prefix(self):
        bare = MINIMAX_MODEL.removeprefix("openrouter/")
        self.assertEqual(display_name_for_model(bare), MINIMAX_DISPLAY)

    def test_byo_canonical_id_uses_mapped_display_name(self):
        self.assertEqual(display_name_for_model(ANTHROPIC_SONNET_MODEL), ANTHROPIC_SONNET_DISPLAY)

    def test_byo_dotted_variant_uses_mapped_display_name(self):
        # OpenRouter occasionally reports the dotted version; we should still
        # resolve it to the canonical display name.
        self.assertEqual(display_name_for_model("anthropic/claude-sonnet-4.6"), ANTHROPIC_SONNET_DISPLAY)
        self.assertEqual(display_name_for_model("anthropic/claude-opus-4.7"), ANTHROPIC_OPUS_DISPLAY)

    def test_unknown_model_falls_back_to_raw_id(self):
        # Better to show "openai/gpt-4o-mini" than "Unknown Model" — the raw
        # id is at least diagnostic. Adding a new provider should not regress
        # the per-model breakdown UI.
        self.assertEqual(display_name_for_model("openai/gpt-4o-mini"), "openai/gpt-4o-mini")

    def test_empty_string_returns_unknown_model(self):
        self.assertEqual(display_name_for_model(""), "Unknown Model")
