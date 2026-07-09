"""Coverage for the model-attribution helpers shared by every usage path.

These guard the bug where every usage row was tagged as the ``openclaw``
request-side placeholder instead of the upstream model id, collapsing
"Usage by Model" into a single undifferentiated bucket.
"""

from django.test import SimpleTestCase

from .constants import (
    ANTHROPIC_HAIKU_DISPLAY,
    ANTHROPIC_HAIKU_MODEL,
    ANTHROPIC_OPUS_DISPLAY,
    ANTHROPIC_SONNET_DISPLAY,
    ANTHROPIC_SONNET_MODEL,
    DEEPSEEK_MODEL,
    KIMI_MODEL,
    MINIMAX_DISPLAY,
    MINIMAX_MODEL,
    canonical_model_id,
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

    def test_haiku_canonical_id_uses_mapped_display_name(self):
        # Haiku is now registered (platform-metered via OpenRouter), so the
        # rollup labels it instead of falling through to the raw id.
        self.assertEqual(display_name_for_model(ANTHROPIC_HAIKU_MODEL), ANTHROPIC_HAIKU_DISPLAY)


class CanonicalModelIdTest(SimpleTestCase):
    def test_strips_openrouter_prefix(self):
        self.assertEqual(canonical_model_id(DEEPSEEK_MODEL), DEEPSEEK_MODEL.removeprefix("openrouter/"))

    def test_dotted_version_becomes_hyphenated(self):
        self.assertEqual(canonical_model_id("anthropic/claude-haiku-4.5"), "anthropic/claude-haiku-4-5")
        self.assertEqual(canonical_model_id("anthropic/claude-sonnet-4.6"), "anthropic/claude-sonnet-4-6")

    def test_two_spellings_map_to_same_canonical(self):
        # The exact bug from the screenshot: two haiku spellings, one model.
        self.assertEqual(
            canonical_model_id("anthropic/claude-haiku-4.5"),
            canonical_model_id("anthropic/claude-haiku-4-5"),
        )
        self.assertEqual(canonical_model_id("anthropic/claude-haiku-4-5"), ANTHROPIC_HAIKU_MODEL)

    def test_lowercases_and_trims(self):
        self.assertEqual(
            canonical_model_id("  OpenRouter/DeepSeek/DeepSeek-V4-Pro  "),
            "deepseek/deepseek-v4-pro",
        )

    def test_already_canonical_is_idempotent(self):
        canon = canonical_model_id("anthropic/claude-haiku-4.5")
        self.assertEqual(canonical_model_id(canon), canon)

    def test_letter_prefixed_dot_is_left_untouched(self):
        # ``minimax-m2.7`` has a dot after a letter, not a hyphenated version
        # token like ``-4.5`` — it must not be rewritten.
        self.assertEqual(canonical_model_id(MINIMAX_MODEL), MINIMAX_MODEL.removeprefix("openrouter/"))
        self.assertEqual(canonical_model_id("openrouter/minimax/minimax-m2.7"), "minimax/minimax-m2.7")

    def test_empty_string(self):
        self.assertEqual(canonical_model_id(""), "")
