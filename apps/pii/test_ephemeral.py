"""The non-persisting egress lane must mask unknown PII without changing chat."""

import copy
import json
import re
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.pii.ephemeral import redact_texts_ephemeral_checked
from apps.pii.shared_client import SharedPiiPipeline


def detector(text):
    return [
        {"entity_group": "FIRSTNAME", "start": match.start(), "end": match.end(), "score": 0.99}
        for match in re.finditer("Fakenamealpha", text)
    ]


class EphemeralRedactionTests(SimpleTestCase):
    def setUp(self):
        self.tenant = SimpleNamespace(
            model_tier="starter",
            pii_entity_map={"[PERSON_2]": {"name": "Knownfixture"}},
            pii_type_counters={"PERSON": 7},
            pii_denylist={},
            user=SimpleNamespace(display_name="Fakenamealpha"),
        )
        self.enterContext(patch("apps.pii.engine.get_pattern_recognizers", return_value={}))

    def redact(self, texts):
        return redact_texts_ephemeral_checked(texts, self.tenant, deadline=monotonic() + 2)

    def test_known_unknown_and_owner_names_masked_without_mutation(self):
        before = copy.deepcopy(vars(self.tenant))
        with patch("apps.pii.engine.get_pii_pipeline", return_value=detector):
            outcomes = self.redact(["Knownfixture and Fakenamealpha", "Fakenamealpha again"])
        self.assertTrue(all(outcome.confirmed for outcome in outcomes))
        self.assertEqual([o.text for o in outcomes], ["[PERSON_2] and [PERSON_8]", "[PERSON_8] again"])
        self.assertEqual(vars(self.tenant), before)

    def test_new_name_reused_even_when_detector_misses_other_mention(self):
        with patch("apps.pii.engine.get_pii_pipeline", return_value=lambda text: detector(text)[:1]):
            outcomes = self.redact(["Fakenamealpha", "Fakenamealpha"])
        self.assertEqual([o.text for o in outcomes], ["[PERSON_8]", "[PERSON_8]"])

    def test_detector_failure_or_cross_field_span_never_confirms(self):
        for pipeline in (
            Mock(side_effect=RuntimeError("private sentinel")),
            lambda text: [{"entity_group": "FIRSTNAME", "start": 0, "end": len(text), "score": 0.99}],
        ):
            with (
                self.subTest(pipeline=type(pipeline).__name__),
                patch("apps.pii.engine.get_pii_pipeline", return_value=pipeline),
                self.assertNoLogs("apps.pii.ephemeral", level="DEBUG"),
            ):
                outcomes = self.redact(["Fakenamealpha", "Fakenamealpha"])
                self.assertTrue(all(not outcome.confirmed for outcome in outcomes))
                self.assertTrue(all(outcome.text == "" for outcome in outcomes))

    def test_expired_deadline_does_not_start_detector(self):
        with patch("apps.pii.engine.get_pii_pipeline") as get_pipeline, self.assertRaises(TimeoutError):
            redact_texts_ephemeral_checked(["message"], self.tenant, deadline=monotonic() - 1)
        get_pipeline.assert_not_called()

    def test_shared_detector_receives_remaining_budget_in_socket_and_frame(self):
        pipeline = SharedPiiPipeline(deadline_s=5)
        response = {"v": 1, "engine": pipeline.engine, "spans": []}
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=pipeline),
            patch("apps.pii.shared_client.socket.socket") as socket,
            patch("apps.pii.shared_client._decode_response", return_value=response),
        ):
            outcome = redact_texts_ephemeral_checked(["text"], self.tenant, deadline=monotonic() + 0.5)[0]
        self.assertTrue(outcome.confirmed)
        frame = socket.return_value.sendall.call_args.args[0]
        self.assertLessEqual(json.loads(frame[4:])["ttl_ms"], 500)
        for call in socket.return_value.settimeout.call_args_list:
            self.assertGreater(call.args[0], 0)
            self.assertLessEqual(call.args[0], 0.5)
        socket.return_value.close.assert_called_once()

    def test_short_deadline_failure_does_not_open_normal_chat_breaker(self):
        from apps.pii.shared_client import SharedPiiError

        pipeline = SharedPiiPipeline(deadline_s=5)
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=pipeline),
            patch.object(SharedPiiPipeline, "_call", side_effect=SharedPiiError("timeout", outcome="timeout")),
        ):
            for _ in range(3):
                with self.assertRaises(TimeoutError):
                    self.redact(["text"])
            self.assertEqual(pipeline._consecutive_failures, 0)
            self.assertEqual(pipeline._open_until, 0)
            # The existing default call still records its own failures.
            with self.assertRaises(SharedPiiError):
                pipeline("text")
            self.assertEqual(pipeline._consecutive_failures, 1)

    def test_long_batch_masks_tail_without_detector_window_truncation(self):
        calls = []

        def windowed_detector(text):
            calls.append(text)
            return detector(text[:384])

        with patch("apps.pii.engine.get_pii_pipeline", return_value=windowed_detector):
            outcomes = self.redact(["ordinary words " * 80, "Fakenamealpha"])
        self.assertTrue(all(outcome.confirmed for outcome in outcomes))
        self.assertEqual(outcomes[-1].text, "[PERSON_8]")
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(len(text.encode("utf-8")) <= 384 for text in calls))

    def test_unbroken_token_beyond_window_fails_closed(self):
        with patch("apps.pii.engine.get_pii_pipeline") as pipeline:
            outcomes = self.redact(["a" * 500])
        self.assertFalse(outcomes[0].confirmed)
        pipeline.assert_not_called()

    def test_conflicting_window_boundaries_fail_closed(self):
        text = "words " * 60 + "Fakenamealpha Fakenamebeta " + "words " * 20

        def partial_detector(chunk):
            start = chunk.find("Fakenamealpha")
            if start < 0:
                return []
            end = start + len("Fakenamealpha")
            if "Fakenamealpha Fakenamebeta" in chunk:
                end += len(" Fakenamebeta")
            return [{"entity_group": "FIRSTNAME", "start": start, "end": end, "score": 0.99}]

        with patch("apps.pii.engine.get_pii_pipeline", return_value=partial_detector):
            outcomes = self.redact([text])
        self.assertFalse(outcomes[0].confirmed)
