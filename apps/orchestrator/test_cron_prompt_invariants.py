"""Regression tests pinning prompt-prefix invariants for the cron patcher.

`update_system_cron_prompts` only swaps in a new prompt body when the existing
job's message starts with one of the strings in `_KNOWN_DEFAULT_PREFIXES`. If a
default prompt is edited so its leading text no longer matches any prefix,
existing tenants stay stuck on the old body — the patcher silently treats them
as user-customized. This test fails fast when that invariant breaks.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.orchestrator import config_generator
from apps.orchestrator.services import update_system_cron_prompts


def _known_prefixes() -> list[str]:
    """Pull the prefix list out of the patcher's enclosing scope.

    `_KNOWN_DEFAULT_PREFIXES` is defined inside `update_system_cron_prompts`,
    not module-level. Cheapest reliable access is to read the source.
    """
    import inspect

    source = inspect.getsource(update_system_cron_prompts)
    start = source.index("_KNOWN_DEFAULT_PREFIXES = [")
    end = source.index("]", start)
    block = source[start:end]
    prefixes: list[str] = []
    for line in block.splitlines():
        line = line.strip()
        if line.startswith('"') and line.endswith('",'):
            prefixes.append(line[1:-2])
        elif line.startswith('"') and line.endswith('"'):
            prefixes.append(line[1:-1])
    return prefixes


class CronPromptPrefixInvariantTest(SimpleTestCase):
    def setUp(self):
        self.prefixes = _known_prefixes()
        self.assertTrue(self.prefixes, "Failed to extract _KNOWN_DEFAULT_PREFIXES")

    def _assert_starts_with_known_prefix(self, name: str, body: str) -> None:
        self.assertTrue(
            any(body.startswith(prefix) for prefix in self.prefixes),
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
