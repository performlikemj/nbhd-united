from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.pii.authoring import author_json_paths, author_text, resolve_receipt_values, truncate_placeholder_safe
from apps.pii.redactor import MINT_ALL, MINT_NEVER, MINT_VALIDATED, RedactionOutcome
from apps.pii.repair_sweep import REPAIR_STATES
from apps.pii.store_registry import registered_store
from apps.tenants.models import Tenant, User


class AuthorTextTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="authoring", password="x")
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            pii_entity_map={"[PERSON_1]": {"name": "Alice"}},
        )

    def test_flag_off_owner_preserves_legacy_redaction_without_checked_receipt(self):
        original = "  Alice\n[PERSON_999]  "
        before_map = dict(self.tenant.pii_entity_map)
        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.authoring.redact_user_message_checked") as checked,
            patch("apps.pii.authoring._redact_active_known_values") as known_values,
            patch("apps.pii.authoring._residual_summary") as residual,
        ):
            authored = author_text(
                self.tenant,
                original,
                seam="test.flag-off",
                writer="owner",
                field="title",
            )

        self.assertEqual(authored.text, "  [PERSON_1]\n[PERSON_999]  ")
        self.assertEqual(
            authored.receipt,
            {"state": "bypass", "mode": "legacy-redact", "writer": "owner"},
        )
        checked.assert_not_called()
        known_values.assert_not_called()
        residual.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map, before_map)

    def test_flag_off_runtime_and_background_are_passthrough_without_redaction_calls(self):
        original = "  Alice\n[PERSON_999]  "
        with (
            patch("apps.pii.authoring.redact_user_message") as legacy,
            patch("apps.pii.authoring.redact_user_message_checked") as checked,
            patch("apps.pii.authoring._redact_active_known_values") as known_values,
            patch("apps.pii.authoring._residual_summary") as residual,
        ):
            for writer in ("runtime", "background"):
                with self.subTest(writer=writer):
                    authored = author_text(
                        self.tenant,
                        original,
                        seam="test.flag-off",
                        writer=writer,
                        field="title",
                    )
                    self.assertEqual(authored.text, original)
                    self.assertEqual(authored.receipt, {"state": "bypass", "writer": writer})

        legacy.assert_not_called()
        checked.assert_not_called()
        known_values.assert_not_called()
        residual.assert_not_called()

    def test_placeholder_safe_truncation_never_bisects_token(self):
        text = "12345[PERSON_123]tail"
        self.assertEqual(truncate_placeholder_safe(text, 10), "12345")
        self.assertEqual(truncate_placeholder_safe(text, len("12345[PERSON_123]")), "12345[PERSON_123]")
        self.assertEqual(truncate_placeholder_safe("abcdefgh", 5), "abcde")

    def test_writer_classes_apply_the_directive_mint_policies(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(text="clean", confirmed=True, reason="redacted"),
            ) as checked,
            patch("apps.pii.authoring._redact_active_known_values", side_effect=lambda _t, text, **_kw: text),
            patch("apps.pii.authoring._residual_summary", return_value={"count": 0, "kinds": {}}),
        ):
            for writer, mint, allow_user_name in (
                ("owner", MINT_ALL, True),
                ("runtime", MINT_NEVER, False),
                ("background", MINT_VALIDATED, False),
            ):
                with self.subTest(writer=writer):
                    authored = author_text(self.tenant, "text", seam="test.policy", writer=writer, field="title")
                    self.assertEqual(checked.call_args.kwargs["mint"], mint)
                    self.assertEqual(checked.call_args.kwargs["allow_user_name"], allow_user_name)
                    self.assertEqual(authored.receipt["writer"], writer)

    def test_background_records_unknown_person_residual_without_value(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(
                    text="[PERSON_1] met Unknown Person",
                    confirmed=True,
                    reason="redacted",
                ),
            ),
            patch(
                "apps.pii.authoring._residual_summary",
                return_value={"count": 1, "kinds": {"PERSON": 1}},
            ),
        ):
            authored = author_text(
                self.tenant,
                "Alice met Unknown Person",
                seam="test.background",
                writer="background",
                field="description",
            )

        self.assertEqual(authored.text, "[PERSON_1] met Unknown Person")
        self.assertEqual(authored.receipt["state"], "residual")
        self.assertEqual(authored.receipt["residual_spans"], {"count": 1, "kinds": {"PERSON": 1}})
        self.assertNotIn("Unknown Person", repr(authored.receipt))

    def test_runtime_records_unknown_person_residual_without_minting(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        before_map = dict(self.tenant.pii_entity_map)
        model_output = "[PERSON_1] is meeting Dana Whitfield"
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(text=model_output, confirmed=True, reason="redacted"),
            ) as checked,
            patch(
                "apps.pii.authoring._residual_summary",
                return_value={"count": 1, "kinds": {"PERSON": 1}},
            ),
        ):
            authored = author_text(
                self.tenant,
                model_output,
                seam="test.runtime",
                writer="runtime",
                field="description",
            )

        self.assertEqual(authored.text, model_output)
        self.assertEqual(authored.receipt["state"], "residual")
        self.assertEqual(authored.receipt["residual_spans"], {"count": 1, "kinds": {"PERSON": 1}})
        self.assertEqual(authored.receipt["writer"], "runtime")
        self.assertNotIn("Dana Whitfield", repr(authored.receipt))
        self.assertEqual(checked.call_args.kwargs["mint"], MINT_NEVER)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map, before_map)

    def test_runtime_clean_placeholder_space_text_stays_placeholder(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        model_output = "[PERSON_1] is on track"
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(text=model_output, confirmed=True, reason="redacted"),
            ),
            patch("apps.pii.authoring._residual_summary", return_value={"count": 0, "kinds": {}}),
        ):
            authored = author_text(
                self.tenant,
                model_output,
                seam="test.runtime",
                writer="runtime",
                field="description",
            )

        self.assertEqual(authored.text, model_output)
        self.assertEqual(authored.receipt["state"], "placeholder")
        self.assertNotIn("residual_spans", authored.receipt)

    def test_flag_off_runtime_never_reaches_the_detector(self):
        original = "Dana Whitfield is meeting Alice"
        with (
            patch("apps.pii.redactor._detect_pii") as detect,
            patch("apps.pii.authoring._residual_summary") as residual,
        ):
            authored = author_text(
                self.tenant,
                original,
                seam="test.flag-off",
                writer="runtime",
                field="description",
            )

        self.assertEqual(authored.text, original)
        self.assertEqual(authored.receipt, {"state": "bypass", "writer": "runtime"})
        detect.assert_not_called()
        residual.assert_not_called()

    def test_redaction_error_uses_independent_known_value_path_and_marks_repair(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        with patch(
            "apps.pii.authoring.redact_user_message_checked",
            return_value=RedactionOutcome(text="Alice failed", confirmed=False, reason="redaction-error"),
        ):
            authored = author_text(
                self.tenant,
                "Alice failed",
                seam="test.failure",
                writer="owner",
                field="title",
            )

        self.assertEqual(authored.text, "[PERSON_1] failed")
        self.assertEqual(authored.receipt["state"], "unconfirmed")
        self.assertEqual(authored.receipt["reason"], "redaction-error")
        self.assertEqual(
            authored.receipt["redactions"],
            [{"placeholder": "[PERSON_1]"}],
        )

    def test_owner_receipt_resolution_accepts_both_shapes_and_prefers_live_map(self):
        receipts = {
            "legacy": {
                "state": "placeholder",
                "redactions": [{"placeholder": "[PERSON_1]", "value": "Stale Alice"}],
            },
            "current": {
                "state": "placeholder",
                "redactions": [{"placeholder": "[PERSON_1]"}],
            },
        }

        live_map = {"[PERSON_1]": {"name": "Renamed Alice", "retired": True}}
        resolved = resolve_receipt_values(receipts, live_map)

        expected = [{"placeholder": "[PERSON_1]", "value": "Renamed Alice"}]
        self.assertEqual(resolved["legacy"]["redactions"], expected)
        self.assertEqual(resolved["current"]["redactions"], expected)

    def test_authoring_centrally_truncates_registered_charfield_without_splitting_placeholder(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        poison = "x" * 250 + "[PERSON_123456789]" + "tail"
        with patch(
            "apps.pii.authoring.redact_user_message_checked",
            return_value=RedactionOutcome(text=poison, confirmed=True, reason="redacted"),
        ):
            authored = author_text(
                self.tenant,
                "safe source",
                seam="test.truncate",
                writer="owner",
                field="title",
                model_label="journal.Task",
            )

        self.assertEqual(authored.text, "x" * 250)
        self.assertLessEqual(len(authored.text), 256)
        self.assertNotIn("[PERSON_", authored.text)
        self.assertEqual(authored.receipt["redactions"], [])

    def test_live_write_error_rate_fires_metadata_only_alert(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        outcomes = [RedactionOutcome(text="clean", confirmed=True, reason="redacted")] * 19
        outcomes.append(RedactionOutcome(text="private Alice", confirmed=False, reason="redaction-error"))

        with (
            patch("apps.pii.authoring.redact_user_message_checked", side_effect=outcomes),
            patch("apps.transcripts.alerts._send_alert", return_value=True) as send_alert,
        ):
            for _ in range(20):
                author_text(
                    self.tenant,
                    "private Alice",
                    seam="test.live-write",
                    writer="owner",
                    field="description",
                )

        send_alert.assert_called_once()
        body = send_alert.call_args.kwargs["body"]
        self.assertIn("Seam: test.live-write", body)
        self.assertIn("Writer class: owner", body)
        self.assertIn("Writer attempts: 20", body)
        self.assertIn("Writer errors: 1", body)
        self.assertNotIn("private Alice", body)

    def test_residual_detector_error_keeps_already_redacted_output(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(
                    text="[PERSON_1] emailed [EMAIL_ADDRESS_2]",
                    confirmed=True,
                    reason="redacted",
                ),
            ),
            patch("apps.pii.authoring._residual_summary", side_effect=RuntimeError("detector down")),
        ):
            authored = author_text(
                self.tenant,
                "Alice emailed private@example.com",
                seam="test.residual-failure",
                writer="background",
                field="description",
            )

        self.assertEqual(authored.text, "[PERSON_1] emailed [EMAIL_ADDRESS_2]")
        self.assertEqual(authored.receipt["state"], "unconfirmed")
        self.assertNotIn("private@example.com", authored.text)

    def test_empty_and_disabled_reason_codes_are_not_errors(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        outcomes = (
            (RedactionOutcome(text="", confirmed=False, reason="empty-input"), "placeholder"),
            (RedactionOutcome(text="raw", confirmed=False, reason="redaction-disabled"), "bypass"),
        )
        with patch("apps.pii.alerts.record_live_write_outcome") as record_live:
            for outcome, expected_state in outcomes:
                with (
                    self.subTest(reason=outcome.reason),
                    patch(
                        "apps.pii.authoring.redact_user_message_checked",
                        return_value=outcome,
                    ),
                ):
                    authored = author_text(
                        self.tenant,
                        outcome.text,
                        seam="test.non-error",
                        writer="owner",
                        field="description",
                    )
                    self.assertEqual(authored.receipt["state"], expected_state)
                    self.assertNotEqual(authored.receipt["state"], "unconfirmed")
        record_live.assert_not_called()

    def test_nonempty_json_shape_mismatch_is_unconfirmed_for_repair(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        paths = registered_store("journal.Purpose").nested_json_paths("evidence")

        with patch("apps.pii.alerts.record_live_write_outcome") as record_live:
            authored = author_json_paths(
                self.tenant,
                {"unexpected": "Alice"},
                paths=paths,
                seam="test.json-shape",
                writer="runtime",
                field="payload",
            )

        self.assertEqual(authored.value, {"unexpected": "Alice"})
        self.assertEqual(
            authored.receipt,
            {
                "state": "unconfirmed",
                "reason": "shape-mismatch",
                "redactions": [],
                "writer": "runtime",
            },
        )
        self.assertIn(authored.receipt["state"], REPAIR_STATES)
        record_live.assert_called_once_with(
            self.tenant,
            seam="test.json-shape",
            writer="runtime",
            is_error=True,
        )

    def test_flag_off_json_shape_mismatch_is_byte_identical_bypass(self):
        paths = registered_store("journal.Purpose").nested_json_paths("evidence")
        original = {"unexpected": "  Alice\n[PERSON_999]  "}

        with (
            patch("apps.pii.alerts.record_live_write_outcome") as record_live,
            patch("apps.pii.authoring.redact_user_message_checked") as checked,
        ):
            authored = author_json_paths(
                self.tenant,
                original,
                paths=paths,
                seam="test.json-shape-off",
                writer="runtime",
                field="payload",
            )

        self.assertIs(authored.value, original)
        self.assertEqual(authored.value, {"unexpected": "  Alice\n[PERSON_999]  "})
        self.assertEqual(authored.receipt, {"state": "bypass", "writer": "runtime"})
        checked.assert_not_called()
        record_live.assert_not_called()

    def test_recursive_json_with_no_string_descendants_is_clean_empty_content(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        paths = registered_store("fuel.Workout").nested_json_paths("detail_json")
        original = {"sets": [{"reps": 5, "complete": True}], "score": 3.5, "note": None}

        with patch("apps.pii.alerts.record_live_write_outcome") as record_live:
            authored = author_json_paths(
                self.tenant,
                original,
                paths=paths,
                seam="test.recursive-non-text",
                writer="runtime",
                field="detail_json",
                model_label="fuel.Workout",
            )

        self.assertIs(authored.value, original)
        self.assertEqual(
            authored.receipt,
            {
                "state": "placeholder",
                "reason": "empty-input",
                "redactions": [],
                "writer": "runtime",
            },
        )
        record_live.assert_not_called()

    def test_truly_empty_json_keeps_empty_input_receipt(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        paths = registered_store("journal.Purpose").nested_json_paths("evidence")

        authored = author_json_paths(
            self.tenant,
            [],
            paths=paths,
            seam="test.json-empty",
            writer="runtime",
            field="payload",
        )

        self.assertEqual(authored.value, [])
        self.assertEqual(authored.receipt["state"], "placeholder")
        self.assertEqual(authored.receipt["reason"], "empty-input")
        self.assertEqual(authored.receipt["redactions"], [])

    def test_degraded_cache_returning_none_never_fails_the_write(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        # django-redis with IGNORE_EXCEPTIONS=True returns None instead of
        # raising, so LocMem cannot reproduce this — the mock is the only way.
        degraded = SimpleNamespace(
            add=lambda *a, **kw: None,
            incr=lambda *a, **kw: None,
            get=lambda *a, **kw: None,
            set=lambda *a, **kw: None,
        )
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(text="Alice failed", confirmed=False, reason="redaction-error"),
            ),
            patch("apps.pii.alerts.cache", degraded),
            patch("apps.transcripts.alerts._send_alert") as send_alert,
        ):
            authored = author_text(
                self.tenant,
                "Alice failed",
                seam="test.degraded-cache",
                writer="owner",
                field="title",
            )

        self.assertEqual(authored.text, "[PERSON_1] failed")
        self.assertEqual(authored.receipt["state"], "unconfirmed")
        send_alert.assert_not_called()

    def test_alert_gate_cooldown_skips_the_steward_query_once_tripped(self):
        from apps.pii.alerts import send_rate_alert

        with patch("apps.transcripts.alerts._send_alert", return_value=True) as send_alert:
            first = send_rate_alert(
                self.tenant,
                attempts=100,
                count=50,
                kind="error",
                fingerprint_scope="cooldown-scope",
                window="test window",
                counters=(("Attempts", 100),),
            )
            second = send_rate_alert(
                self.tenant,
                attempts=100,
                count=50,
                kind="error",
                fingerprint_scope="cooldown-scope",
                window="test window",
                counters=(("Attempts", 100),),
            )

        self.assertTrue(first)
        self.assertFalse(second)
        send_alert.assert_called_once()

    def test_input_already_over_the_limit_is_left_for_serializer_validation(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        over_limit_input = "y" * 300
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(text=over_limit_input, confirmed=True, reason="redacted"),
            ),
            patch("apps.pii.authoring._residual_summary", return_value={"count": 0, "kinds": {}}),
        ):
            authored = author_text(
                self.tenant,
                over_limit_input,
                seam="test.oversize-input",
                writer="owner",
                field="title",
                model_label="journal.Task",
            )

        self.assertEqual(authored.text, over_limit_input)
        self.assertEqual(len(authored.text), 300)

    def test_flag_off_bypass_never_truncates(self):
        original = "z" * 400
        authored = author_text(
            self.tenant,
            original,
            seam="test.bypass-truncation",
            writer="runtime",
            field="title",
        )

        self.assertEqual(authored.text, original)
        self.assertEqual(authored.receipt["state"], "bypass")

    def test_non_live_authoring_stays_out_of_live_write_counters(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(text="Alice failed", confirmed=False, reason="redaction-error"),
            ),
            patch("apps.pii.alerts.record_live_write_outcome") as record_live,
        ):
            authored = author_text(
                self.tenant,
                "Alice failed",
                seam="test.not-live",
                writer="background",
                field="title",
                live=False,
            )

        self.assertEqual(authored.receipt["state"], "unconfirmed")
        record_live.assert_not_called()

    def test_unresolved_placeholder_omits_the_value_key_entirely(self):
        receipts = {
            "title": {
                "state": "placeholder",
                "redactions": [
                    {"placeholder": "[PERSON_1]", "value": "Stale Alice"},
                    {"placeholder": "[PERSON_404]", "value": "Stale Ghost"},
                ],
            }
        }

        resolved = resolve_receipt_values(receipts, {"[PERSON_1]": {"name": "Live Alice"}})

        redactions = resolved["title"]["redactions"]
        self.assertEqual(redactions[0], {"placeholder": "[PERSON_1]", "value": "Live Alice"})
        self.assertEqual(redactions[1], {"placeholder": "[PERSON_404]"})
        self.assertNotIn("value", redactions[1])
        self.assertNotIn("Stale Ghost", repr(resolved))

    def test_residual_detection_reads_the_stored_text_not_the_raw_input(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(text="Call [PERSON_1]", confirmed=True, reason="redacted"),
            ),
            patch(
                "apps.pii.authoring._residual_summary",
                return_value={"count": 0, "kinds": {}},
            ) as residual,
        ):
            authored = author_text(
                self.tenant,
                "Call Alice",
                seam="test.residual-input",
                writer="runtime",
                field="title",
            )

        # Scanning the raw input re-detects names the redactor already replaced,
        # and an over-captured span ("Call Alice") never matches a known binding.
        self.assertEqual(residual.call_args.args[1], "Call [PERSON_1]")
        self.assertEqual(authored.receipt["state"], "placeholder")

    def test_known_name_in_an_over_captured_span_is_not_false_residual(self):
        """End-to-end with the REAL detector: 'Call Alice' comes back as one
        PERSON span, so matching by exact value cannot find the Alice binding."""
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])

        authored = author_text(
            self.tenant,
            "Call Alice",
            seam="test.over-captured-span",
            writer="runtime",
            field="title",
        )

        self.assertEqual(authored.text, "Call [PERSON_1]")
        self.assertEqual(authored.receipt["state"], "placeholder")
        self.assertNotIn("residual_spans", authored.receipt)

    def test_management_command_toggles_flag(self):
        out = StringIO()
        call_command("set_layer1_placeholder_writes", str(self.tenant.id), "--on", stdout=out)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.layer1_placeholder_writes)
        call_command("set_layer1_placeholder_writes", str(self.tenant.id), "--off", stdout=out)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.layer1_placeholder_writes)
