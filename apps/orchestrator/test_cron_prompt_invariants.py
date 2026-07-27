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


class YesterdaysSignalsToolInPromptsTest(SimpleTestCase):
    """The Personal Question + Heartbeat prompts must instruct the agent to
    call ``nbhd_yesterdays_signals``. Without the call the cron-time
    signal map never materializes, and option (f) in PQ + sub-bullet 5
    in HB become no-ops.

    If the tool is renamed or moved, this test breaks and reminds the
    author to update both prompts. If a future refactor inlines the
    signals via a plugin hook instead, delete this test.
    """

    def test_personal_question_prompt_calls_signals_tool(self):
        self.assertIn(
            "nbhd_yesterdays_signals",
            config_generator._PERSONAL_QUESTION_PROMPT,
            "Personal Question prompt no longer instructs the agent to call "
            "nbhd_yesterdays_signals — signal-driven option (f) in step 3 "
            "becomes a no-op.",
        )
        self.assertIn(
            "notable_gaps",
            config_generator._PERSONAL_QUESTION_PROMPT,
            "Personal Question prompt no longer mentions notable_gaps — the "
            "agent has no contract for what the tool returns.",
        )

    def test_heartbeat_prompt_calls_signals_tool(self):
        self.assertIn(
            "nbhd_yesterdays_signals",
            config_generator._HEARTBEAT_CHECKIN_PROMPT,
            "Heartbeat prompt no longer instructs the agent to call "
            "nbhd_yesterdays_signals — sub-bullet 5 of step 1 becomes a "
            "no-op.",
        )

    def test_heartbeat_prompt_forbids_signal_asking(self):
        # The whole point of giving HB signal awareness without asking
        # authority. If this guard rail disappears, HB will start asking
        # questions and compete with PQ's once-daily dedup'd cadence.
        body = config_generator._HEARTBEAT_CHECKIN_PROMPT
        self.assertIn("DO NOT turn this into a quiz", body)
        self.assertIn("Personal Question cron", body)


class MorningBriefingJournalLinkMarkerTest(SimpleTestCase):
    """The Morning Briefing prompt must instruct the agent to end its
    ``nbhd_send_to_user`` message with a ``[[journal-link: daily|...]]``
    marker so the iOS app renders a tappable "View in Journal" chip
    (backend parse/persist shipped in PR #1182; the marker syntax is
    owned by ``apps.router.journal_link``). If this instruction is
    dropped, the briefing stops emitting the chip and the feature
    silently regresses on the legacy cron path.
    """

    def test_morning_briefing_prompt_emits_journal_link_marker(self):
        body = config_generator._MORNING_BRIEFING_PROMPT_TEMPLATE
        # The literal marker template the agent is told to emit. The `daily`
        # kind + `<DATE>` slug placeholder must match apps.router.journal_link's
        # parser (kind in Document.Kind, slug is the daily-note ISO date).
        self.assertIn("[[journal-link: daily|<DATE>|Morning Report]]", body)
        # And the guardrails that keep the emitted marker parseable: it must be
        # the last line alone, and the date must be the real daily-note slug.
        self.assertIn("must be the very last line", body)
        self.assertIn("must be the daily note's actual slug", body)


class ProactiveQuickRepliesMarkerTest(SimpleTestCase):
    def _assert_quick_reply_contract(self, body: str, journal_marker: str) -> None:
        quick_marker = "[[quick-replies:"
        self.assertIn(
            "1-3 short labels (≤24 characters each; longer labels invalidate the whole marker, so prefer 2-3 words)",
            body,
        )
        self.assertIn("sent verbatim as the user's message when tapped", body)
        self.assertIn("clearly actionable next move", body)
        self.assertIn("quick-replies marker must be the very last line", body)
        self.assertIn("delivery parser strips it first", body)
        self.assertIn("Do not reverse these two marker lines", body)
        self.assertLess(
            body.rindex(journal_marker),
            body.rindex(quick_marker),
            "The prompt's concrete message tail must put journal-link before the final quick-replies marker.",
        )

    def test_morning_briefing_offers_optional_next_moves_in_parse_order(self):
        body = config_generator._MORNING_BRIEFING_PROMPT_TEMPLATE
        self._assert_quick_reply_contract(
            body,
            "[[journal-link: daily|<DATE>|Morning Report]]",
        )
        self.assertIn("How's my week?", body)
        self.assertIn("Add a note", body)
        self.assertIn("Move tomorrow's session", body)

    def test_evening_checkin_offers_optional_next_moves_in_parse_order(self):
        body = config_generator._EVENING_CHECKIN_PROMPT
        self._assert_quick_reply_contract(
            body,
            "[[journal-link: daily|<DATE>|Evening Check-in]]",
        )
        self.assertIn("👍 Good day", body)
        self.assertIn("🫤 Mixed", body)
        self.assertIn("👎 Rough", body)
