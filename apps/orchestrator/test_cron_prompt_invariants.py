"""Regression tests pinning prompt-prefix invariants for the seed refresher.

``refresh_system_cron_rows_from_seed`` only overwrites a row's payload
from the current seed when the existing message body (after stripping
the leading ``Current date and time:`` preamble) starts with one of the
strings in ``_KNOWN_DEFAULT_PREFIXES``. If a default prompt is edited so
its leading text no longer matches any prefix, existing tenants stay
stuck on the old body — the refresher silently treats them as
user-customized. This test fails fast when that invariant breaks.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.orchestrator import config_generator
from apps.orchestrator.services import _KNOWN_DEFAULT_PREFIXES


class CronPromptPrefixInvariantTest(SimpleTestCase):
    def _assert_starts_with_known_prefix(self, name: str, body: str) -> None:
        self.assertTrue(
            any(body.startswith(prefix) for prefix in _KNOWN_DEFAULT_PREFIXES),
            f"{name} no longer starts with any prefix in _KNOWN_DEFAULT_PREFIXES — "
            f"existing tenants will be treated as user-customized and won't pick up "
            f"the new body. Either keep a known leading substring or add a new entry "
            f"to _KNOWN_DEFAULT_PREFIXES in apps/orchestrator/services.py. "
            f"First 80 chars: {body[:80]!r}",
        )

    def test_evening_checkin_prompt_has_known_prefix(self):
        self._assert_starts_with_known_prefix(
            "_EVENING_CHECKIN_PROMPT",
            config_generator._EVENING_CHECKIN_PROMPT,
        )

    def test_personal_question_prompt_has_known_prefix(self):
        self._assert_starts_with_known_prefix(
            "_PERSONAL_QUESTION_PROMPT",
            config_generator._PERSONAL_QUESTION_PROMPT,
        )
