"""Runs the synthetic PII eval corpus (``apps/pii/eval_corpus.py``) through
``apps.pii.redactor.redact_user_message`` — the canonical seam every inbound
channel calls before a user's text reaches OpenClaw / the LLM provider
(``apps/router/poller.py:1432``, ``apps/router/line_webhook.py:1317-1318``,
``apps/router/chat_views.py:480``). See ``eval_corpus.py``'s module docstring
for the full rationale on why the neural pipeline is mocked (mirrors the
``_fake_pipeline`` convention already established in ``apps/pii/tests.py``)
while Presidio's pattern recognizers (email/phone/card/IBAN) run for real.

Known-gap handling: this repo runs plain ``manage.py test`` (unittest), which
has no ``pytest.mark.xfail``. A corpus case with ``known_gap`` set is asserted
to CURRENTLY FAIL via ``assertRaises(AssertionError)`` — if the underlying
behavior is later fixed, that wrapper itself fails loudly, forcing the corpus
entry to be updated rather than silently drifting stale.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.pii.eval_corpus import CASES, KNOWN_ENTITY_CASES, SEQUENCE_CASES, EvalCase, KnownEntityEvalCase, RawHit
from apps.pii.redactor import (
    _PLACEHOLDER_RE,
    DetectedEntity,
    RedactionSession,
    redact_known_entities,
    redact_user_message,
)
from apps.tenants.services import create_tenant

REQUIRED_TAG_VOCAB = {"incident", "standard", "adversarial", "jp"}


class KnownEntityCorpusTest(SimpleTestCase):
    """Runs ``eval_corpus.KNOWN_ENTITY_CASES`` through
    ``apps.pii.redactor.redact_known_entities`` — the reuse-only entry point
    (no detection, no DB write) used for agent-authored text that must never
    mint a new placeholder. Pure function of ``pii_entity_map``/
    ``pii_denylist``, so a ``SimpleNamespace`` tenant stub is enough (mirrors
    ``test_redact_known_entities.py``'s existing convention) — no DB needed,
    hence ``SimpleTestCase``.
    """

    def _redact(self, case: KnownEntityEvalCase) -> str:
        tenant = SimpleNamespace(pii_entity_map=case.entity_map, pii_denylist=case.denylist)
        return redact_known_entities(tenant, case.text)

    def test_known_entity_cases(self):
        for case in KNOWN_ENTITY_CASES:
            with self.subTest(case_id=case.id, tags=case.tags):
                result = self._redact(case)
                for s in case.expected_redacted:
                    self.assertNotIn(
                        s, result, f"[{case.id}] should be redacted but is still present: {s!r} in {result!r}"
                    )
                for s in case.expected_preserved:
                    self.assertIn(s, result, f"[{case.id}] should be preserved but is missing: {s!r} not in {result!r}")


class PinnedReuseBehaviorTest(SimpleTestCase):
    """A few EXACT-output cases for the reuse-only entry points where a
    substring check would hide the point — folded in from PR #1144. Unlike
    that PR, the same-name-fusion case here is framed as an OPEN gap (not
    "accepted, pinned"), for consistency with SEQUENCE_CASES above and with
    memory/project_pii_same_name_fusion_assessment.md, which records
    structural fixes as ranked-but-NOT-implemented pending an owner decision
    — not as settled behavior.
    """

    def test_longest_name_wins_precedence(self):
        # "Amara Whitfield Jr." must match before the shorter "Amara" entry, or
        # the short name would corrupt it into "[PERSON_2] Whitfield Jr.".
        tenant = SimpleNamespace(
            pii_entity_map={
                "[PERSON_1]": {"name": "Amara Whitfield Jr."},
                "[PERSON_2]": {"name": "Amara"},
            },
            pii_denylist={},
        )
        out = redact_known_entities(tenant, "Amara Whitfield Jr. called Amara")
        self.assertEqual(out, "[PERSON_1] called [PERSON_2]")

    def test_same_name_fuses_via_redaction_session(self):
        # Today: a freshly-DETECTED span whose canonical key already exists in
        # the tenant map reuses that placeholder — even for a person who is
        # (as far as the redactor can tell) a totally different individual
        # sharing the same full name. FLIPS WHEN: RedactionSession/entity_registry
        # gains any disambiguation beyond an exact casefolded string match (see
        # eval_corpus.SEQUENCE_CASES same_name_fusion_two_different_people for
        # the equivalent gap via redact_user_message) — this case already feeds
        # a real DetectedEntity hit for the identical surface string, so any
        # such fix is observed directly as a genuine new mint below.
        tenant = SimpleNamespace(pii_entity_map={"[PERSON_1]": {"name": "Amara Whitfield"}}, pii_denylist={})
        session = RedactionSession(tenant=tenant)
        text = "My cousin, also named Amara Whitfield, is visiting next week"
        start = text.index("Amara Whitfield")
        hit = [DetectedEntity("PERSON", start, start + len("Amara Whitfield"), 0.95)]
        with patch("apps.pii.redactor._detect_pii", return_value=hit):
            out = session.redact(text)

        with self.assertRaises(
            AssertionError,
            msg=(
                "[same_name_fuses_via_redaction_session] marked known_gap but now PASSES — "
                "promote it out of known_gap: a distinct person sharing a name now gets a "
                "genuinely new mint via RedactionSession instead of fusing."
            ),
        ):
            # Desired (currently failing) behavior: a distinct person gets their
            # OWN placeholder and a real new mint is recorded.
            self.assertIn("[PERSON_2]", out)
            self.assertEqual(session.entity_map, {"[PERSON_2]": "Amara Whitfield"})

    def test_distinct_surface_form_does_not_fuse_via_redaction_session(self):
        # Contrast case: a name that does NOT casefold-match anything in the
        # map mints its OWN placeholder rather than fusing — fusion only
        # happens on an exact (casefolded) string match, never a fuzzy one.
        # This is the accidental protection SEQUENCE_CASES documents too, just
        # via RedactionSession directly instead of redact_user_message.
        tenant = SimpleNamespace(pii_entity_map={"[PERSON_1]": {"name": "Amara Whitfield"}}, pii_denylist={})
        session = RedactionSession(tenant=tenant)
        text = "Amara W. is joining the call"
        hit = [DetectedEntity("PERSON", 0, len("Amara W."), 0.95)]
        with patch("apps.pii.redactor._detect_pii", return_value=hit):
            out = session.redact(text)
        self.assertIn("[PERSON_2]", out)
        self.assertNotIn("[PERSON_1]", out)
        self.assertEqual(session.entity_map, {"[PERSON_2]": "Amara W."})


def _pipeline_for_hits(hits: tuple[RawHit, ...]):
    """Fake HF token-classification pipeline driven by corpus ``RawHit``s.

    Same dict shape as ``apps.pii.tests._fake_pipeline`` (``entity_group``/
    ``word``/``score``/``start``/``end``) so ``_detect_pii`` runs the REAL
    label-map + threshold + guard + hygiene logic end to end. Extended with a
    per-word cursor so a hits list can place the SAME word at two distinct
    offsets (e.g. a name mentioned twice in one message) instead of always
    resolving to its first occurrence.
    """

    def _run(text: str):
        out = []
        cursor: dict[str, int] = {}
        for hit in hits:
            start_from = cursor.get(hit.word, 0)
            idx = text.find(hit.word, start_from)
            if idx < 0:
                idx = text.find(hit.word)
            if idx < 0:
                continue
            out.append(
                {
                    "entity_group": hit.raw_label,
                    "word": hit.word,
                    "score": hit.score,
                    "start": idx,
                    "end": idx + len(hit.word),
                }
            )
            cursor[hit.word] = idx + len(hit.word)
        return out

    return _run


class PiiEvalCorpusTest(TestCase):
    """One subTest per ``eval_corpus.CASES`` entry; failures name the case id."""

    def _tenant_for(self, case: EvalCase, chat_id: int):
        return create_tenant(
            display_name=case.tenant_display_name or "Corpus Tester",
            telegram_chat_id=chat_id,
        )

    def _redact(self, case: EvalCase, tenant) -> str:
        if case.seed_entity_map is not None:
            type(tenant).objects.filter(pk=tenant.pk).update(pii_entity_map=case.seed_entity_map)
            tenant.pii_entity_map = dict(case.seed_entity_map)
        with patch(
            "apps.pii.engine.get_pii_pipeline",
            return_value=_pipeline_for_hits(case.raw_hits),
        ):
            return redact_user_message(case.text, tenant)

    def _check(self, case: EvalCase, result: str) -> None:
        if case.expected_exact is not None:
            self.assertEqual(result, case.expected_exact, f"[{case.id}] exact-match mismatch")
        for s in case.expected_redacted:
            self.assertNotIn(s, result, f"[{case.id}] should be redacted but is still present: {s!r} in {result!r}")
        for s in case.expected_preserved:
            self.assertIn(s, result, f"[{case.id}] should be preserved but is missing: {s!r} not in {result!r}")
        if case.expect_clean_placeholders:
            self.assertEqual(
                result.count("["),
                len(_PLACEHOLDER_RE.findall(result)),
                f"[{case.id}] garbled placeholders in {result!r}",
            )
            self.assertNotIn("[[", result, f"[{case.id}] nested placeholder garbling in {result!r}")
            self.assertNotIn("]]", result, f"[{case.id}] nested placeholder garbling in {result!r}")

    def test_corpus_cases(self):
        for i, case in enumerate(CASES):
            with self.subTest(case_id=case.id, tags=case.tags):
                tenant = self._tenant_for(case, chat_id=970000 + i)
                result = self._redact(case, tenant)
                if case.known_gap:
                    with self.assertRaises(
                        AssertionError,
                        msg=(
                            f"[{case.id}] marked known_gap but now PASSES — promote it out of "
                            f"known_gap in eval_corpus.py. Reason on file: {case.known_gap}"
                        ),
                    ):
                        self._check(case, result)
                else:
                    self._check(case, result)


class PiiEvalCorpusSequenceTest(TestCase):
    """Multi-turn corpus cases — same-name fusion is only observable across
    two messages against the SAME tenant's persistent ``pii_entity_map``.
    See ``eval_corpus.SEQUENCE_CASES``.
    """

    def _placeholders(self, text: str) -> list[str]:
        return [m.group(0) for m in _PLACEHOLDER_RE.finditer(text)]

    def test_sequence_cases(self):
        for i, case in enumerate(SEQUENCE_CASES):
            with self.subTest(case_id=case.id, tags=case.tags):
                tenant = create_tenant(display_name="Corpus Tester", telegram_chat_id=980000 + i)
                placeholders_per_turn: list[str] = []
                for turn_text, turn_hits in zip(case.turns, case.turn_raw_hits, strict=True):
                    with patch(
                        "apps.pii.engine.get_pii_pipeline",
                        return_value=_pipeline_for_hits(turn_hits),
                    ):
                        result = redact_user_message(turn_text, tenant)
                    found = self._placeholders(result)
                    self.assertEqual(
                        len(found),
                        1,
                        f"[{case.id}] expected exactly one placeholder in turn, got {found} in {result!r}",
                    )
                    placeholders_per_turn.append(found[0])

                def _check(case=case, placeholders_per_turn=placeholders_per_turn):
                    if case.assertion == "same_placeholder":
                        self.assertEqual(
                            len(set(placeholders_per_turn)),
                            1,
                            f"[{case.id}] expected the SAME placeholder across turns, got {placeholders_per_turn}",
                        )
                    elif case.assertion == "distinct_placeholders":
                        self.assertEqual(
                            len(set(placeholders_per_turn)),
                            len(placeholders_per_turn),
                            f"[{case.id}] expected DISTINCT placeholders across turns, got {placeholders_per_turn}",
                        )
                    else:
                        self.fail(f"[{case.id}] unknown assertion kind {case.assertion!r}")

                if case.known_gap:
                    with self.assertRaises(
                        AssertionError,
                        msg=(
                            f"[{case.id}] marked known_gap but now PASSES — promote it out of "
                            f"known_gap in eval_corpus.py. Reason on file: {case.known_gap}"
                        ),
                    ):
                        _check()
                else:
                    _check()


class EvalCorpusIntegrityTest(SimpleTestCase):
    """Sanity checks on the corpus data itself — catches authoring mistakes
    (duplicate ids, untagged/dead entries) before they reach the redaction
    tests above, and pins the corpus to the size the directive asked for."""

    def test_case_ids_are_unique(self):
        ids = [c.id for c in CASES] + [c.id for c in SEQUENCE_CASES] + [c.id for c in KNOWN_ENTITY_CASES]
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertFalse(dupes, f"duplicate case ids: {dupes}")

    def test_every_case_has_a_recognized_tag(self):
        for case in (*CASES, *SEQUENCE_CASES, *KNOWN_ENTITY_CASES):
            self.assertTrue(
                set(case.tags) & REQUIRED_TAG_VOCAB,
                f"[{case.id}] tags {case.tags} carry none of {REQUIRED_TAG_VOCAB}",
            )

    def test_every_case_asserts_something(self):
        for case in CASES:
            self.assertTrue(
                case.expected_exact is not None or case.expected_redacted or case.expected_preserved,
                f"[{case.id}] asserts nothing — dead corpus entry",
            )
        for case in KNOWN_ENTITY_CASES:
            self.assertTrue(
                case.expected_redacted or case.expected_preserved,
                f"[{case.id}] asserts nothing — dead corpus entry",
            )

    def test_every_case_is_marked_synthetic(self):
        for case in (*CASES, *SEQUENCE_CASES, *KNOWN_ENTITY_CASES):
            self.assertTrue(case.is_synthetic, f"[{case.id}] is_synthetic must be True — every case here is invented")

    def test_corpus_size_within_target_range(self):
        total = len(CASES) + len(SEQUENCE_CASES) + len(KNOWN_ENTITY_CASES)
        self.assertGreaterEqual(total, 80, f"corpus has only {total} cases, target is 80-120")
        self.assertLessEqual(total, 130, f"corpus has {total} cases, target is 80-120")
