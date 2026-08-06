from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.pii.authoring import author_text
from apps.pii.redactor import MINT_ALL, MINT_NEVER, MINT_VALIDATED, RedactionOutcome
from apps.tenants.models import Tenant, User


class AuthorTextTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="authoring", password="x")
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            pii_entity_map={"[PERSON_1]": {"name": "Alice"}},
        )

    def test_flag_off_is_byte_identical_bypass_without_ner_or_map_mutation(self):
        original = "  Alice\n[PERSON_999]  "
        before_map = dict(self.tenant.pii_entity_map)
        with (
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

        self.assertEqual(authored.text, original)
        self.assertEqual(authored.receipt, {"state": "bypass"})
        checked.assert_not_called()
        known_values.assert_not_called()
        residual.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map, before_map)

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
                    author_text(self.tenant, "text", seam="test.policy", writer=writer, field="title")
                    self.assertEqual(checked.call_args.kwargs["mint"], mint)
                    self.assertEqual(checked.call_args.kwargs["allow_user_name"], allow_user_name)

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
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

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

    def test_management_command_toggles_flag(self):
        out = StringIO()
        call_command("set_layer1_placeholder_writes", str(self.tenant.id), "--on", stdout=out)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.layer1_placeholder_writes)
        call_command("set_layer1_placeholder_writes", str(self.tenant.id), "--off", stdout=out)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.layer1_placeholder_writes)
