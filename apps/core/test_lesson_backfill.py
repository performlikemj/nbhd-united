"""DB-free command contracts with real lesson validation and PII registry authoring."""

import json
from datetime import date
from io import StringIO
from unittest.mock import Mock, patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from apps.core.lesson import TRADITIONS, MeditationLesson
from apps.core.management.commands import backfill_meditation_lessons as backfill
from apps.core.models import MeditationSession, MeditationStatus
from apps.tenants.models import Tenant


def answer(**changes):
    lesson = {
        "tradition": "taoist",
        "teaching_slug": "wu-wei",
        "core_teaching": "Act without forcing.",
        "summary": "Notice where unnecessary effort creates tension.",
        "practice": "Soften one area of tension as you breathe out.",
        **changes,
    }
    return {"choices": [{"message": {"content": json.dumps(lesson)}}]}, "test/primary"


@override_settings(CORE_COMPOSE_MODEL="test/primary", OPENROUTER_API_KEY="test-key")
class LessonBackfillTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.tenant = Tenant(core_enabled=True, layer1_placeholder_writes=False)
        self.output = StringIO()
        self.completion = self.enterContext(patch.object(backfill, "chat_completion", return_value=answer()))
        self.sleep = self.enterContext(patch.object(backfill.time, "sleep"))
        self.enterContext(patch.object(backfill, "_SCHEMA_REJECTED_MODELS", set()))
        self.tenant_get = self.enterContext(patch.object(Tenant.objects, "get", return_value=self.tenant))
        self.tenant_filter = self.enterContext(patch.object(Tenant.objects, "filter"))
        self.session_filter = self.enterContext(patch.object(MeditationSession.objects, "filter"))
        self.session_get = self.enterContext(patch.object(MeditationSession.objects, "get"))
        self.rows_by_id = {}
        self.session_get.side_effect = lambda *, id, tenant: self.rows_by_id[id]

    def session(self, **fields):
        session = MeditationSession(
            tenant=fields.pop("tenant", self.tenant),
            date=date(2026, 9, 9),
            **{
                "title": "Release",
                "theme": "Ease",
                "guidance_text": "Let unnecessary effort soften with the breath.",
                "status": MeditationStatus.READY,
                **fields,
            },
        )
        session.save = Mock()
        return session

    def candidate_ids(self, rows):
        self.rows_by_id.update({row.id: row for row in rows})
        return (row.id for row in rows)

    def run_command(self, rows, *args):
        self.session_filter.return_value.order_by.return_value.values_list.return_value = self.candidate_ids(rows)
        call_command("backfill_meditation_lessons", "--tenant", str(self.tenant.id), *args, stdout=self.output)
        return self.output.getvalue()

    def test_dry_run_has_no_calls_or_writes(self):
        session = self.session(pii_receipts={"title": {"state": "bypass"}})
        with patch.object(backfill, "author_store_fields") as author:
            output = self.run_command([session], "--dry-run")
        self.assertIn("scanned=1 written=0 skipped=0 failed=0 eligible=1", output)
        self.assertEqual(session.lesson, {})
        self.assertEqual(session.pii_receipts, {"title": {"state": "bypass"}})
        session.save.assert_not_called()
        author.assert_not_called()
        self.completion.assert_not_called()
        self.sleep.assert_not_called()

    def test_writes_lesson_and_real_registry_receipts(self):
        session = self.session(pii_receipts={"title": {"state": "bypass"}})
        with patch.object(backfill, "author_store_fields", wraps=backfill.author_store_fields) as author:
            output = self.run_command([session])
        self.assertEqual(
            session.lesson,
            MeditationLesson.model_validate_json(answer()[0]["choices"][0]["message"]["content"]).model_dump(),
        )
        self.assertEqual(session.pii_receipts["title"], {"state": "bypass"})
        self.assertEqual(session.pii_receipts["lesson"], {"state": "bypass", "writer": "background"})
        session.save.assert_called_once_with(update_fields=["lesson", "pii_receipts", "updated_at"])
        self.assertEqual(author.call_args.kwargs["model_label"], "core.MeditationSession")
        self.assertEqual(set(author.call_args.args[1]), {"lesson"})
        self.assertIn("scanned=1 written=1 skipped=0 failed=0", output)
        self.session_filter.assert_called_once_with(
            tenant=self.tenant,
            status__in=(MeditationStatus.READY, MeditationStatus.DELIVERED, MeditationStatus.DONE),
            lesson={},
        )
        self.session_filter.return_value.order_by.assert_called_once_with("-date", "-created_at", "-id")
        call = self.completion.call_args
        self.assertEqual(call.args[0], "test/primary")
        self.assertEqual(call.kwargs["response_format"]["json_schema"]["schema"], MeditationLesson.model_json_schema())
        self.assertIn(", ".join(TRADITIONS), call.args[1][0]["content"])

    def test_skips_existing_lesson_and_repeated_run(self):
        session = self.session()
        self.run_command([session])
        lesson = session.lesson.copy()
        self.output = StringIO()
        self.completion.reset_mock()
        session.save.reset_mock()
        output = self.run_command([session])
        self.assertEqual(session.lesson, lesson)
        self.assertIn("written=0 skipped=1 failed=0", output)
        session.save.assert_not_called()
        self.completion.assert_not_called()

    def test_invalid_retry_then_skip_and_continue_without_content_output(self):
        first, second = self.session(), self.session()
        self.completion.side_effect = [answer(tradition="PRIVATE_SENTINEL"), answer(practice=""), answer()]
        output = self.run_command([first, second])
        self.assertEqual(self.completion.call_count, 3)
        self.assertIn("tradition", self.completion.call_args_list[1].args[1][-1]["content"])
        self.assertIn("Return the corrected JSON object only", self.completion.call_args_list[1].args[1][-1]["content"])
        self.assertEqual(len(self.completion.call_args_list[2].args[1]), 2)
        self.assertEqual(first.lesson, {})
        first.save.assert_not_called()
        second.save.assert_called_once()
        self.assertIn("scanned=2 written=1 skipped=0 failed=1", output)
        self.assertNotIn("PRIVATE_SENTINEL", output)
        self.assertNotIn(first.guidance_text, output)

    def test_retry_can_recover_and_redacts_input_and_feedback(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": "PRIVATE_SENTINEL"}
        session = self.session(
            title="PRIVATE_SENTINEL", theme="PRIVATE_SENTINEL", guidance_text="PRIVATE_SENTINEL breathes."
        )
        self.completion.side_effect = [answer(tradition="PRIVATE_SENTINEL"), answer()]
        with patch.object(backfill, "redact_known_values", wraps=backfill.redact_known_values) as redact:
            self.run_command([session])
        self.assertEqual(redact.call_count, 3)
        self.assertTrue(all(call.kwargs["seam"] == "meditation_lesson_backfill" for call in redact.call_args_list))
        for call in self.completion.call_args_list:
            text = json.dumps(call.args[1])
            self.assertNotIn("PRIVATE_SENTINEL", text)
            self.assertIn("[PERSON_1]", text)
        session.save.assert_called_once()

    def test_invalid_json_gets_exactly_one_validation_retry(self):
        invalid = {"choices": [{"message": {"content": "not JSON"}}]}, "test/primary"
        self.completion.return_value = invalid
        output = self.run_command([self.session()])
        self.assertEqual(self.completion.call_count, 2)
        self.assertIn("failed=1", output)

    def test_empty_text_fields_skip_without_retry_or_write(self):
        for field in MeditationLesson.model_fields:
            for empty in ("", "  "):
                with self.subTest(field=field, empty=empty):
                    self.output = StringIO()
                    self.completion.reset_mock()
                    self.completion.return_value = answer(**{field: empty})
                    session = self.session()
                    with patch.object(backfill, "author_store_fields") as author:
                        output = self.run_command([session])
                    self.completion.assert_called_once()
                    author.assert_not_called()
                    session.save.assert_not_called()
                    self.assertEqual(session.lesson, {})
                    self.assertIn("written=0 skipped=0 failed=0 eligible=1 skipped_unsupported=1", output)

    def test_unnamed_practice_is_supported_by_prompt_and_schema(self):
        self.completion.return_value = answer(tradition="other", teaching_slug="unnamed-practice")
        session = self.session()
        output = self.run_command([session])
        prompt = self.completion.call_args.args[1][0]["content"]
        self.assertIn("tradition: other and teaching_slug: unnamed-practice", prompt)
        self.assertIn("using only the narration", prompt)
        self.assertNotIn("empty string", prompt)
        self.assertEqual(session.lesson["teaching_slug"], "unnamed-practice")
        self.assertIn("written=1", output)

    def test_schema_4xx_fallback_is_memoized_per_model(self):
        response = requests.Response()
        response.status_code = 400
        self.completion.side_effect = [requests.HTTPError(response=response), answer(), answer(), answer()]
        self.run_command([self.session(), self.session()])
        self.run_command([self.session()], "--model", "test/other")
        self.assertEqual(
            [call.kwargs["response_format"]["type"] for call in self.completion.call_args_list],
            ["json_schema", "json_object", "json_object", "json_schema"],
        )
        self.assertEqual(self.completion.call_args_list[-1].args[0], "test/other")
        self.assertEqual(self.sleep.call_count, 4)

    def test_transport_failure_skips_without_content_or_extra_retry(self):
        self.completion.side_effect = RuntimeError("PRIVATE_SENTINEL")
        with self.assertLogs(backfill.logger, level="WARNING") as logs:
            output = self.run_command([self.session()])
        self.completion.assert_called_once()
        self.assertIn("written=0 skipped=0 failed=1", output)
        self.assertNotIn("PRIVATE_SENTINEL", output)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("error=RuntimeError", logs.output[0])
        self.assertNotIn("PRIVATE_SENTINEL", logs.output[0])

    def test_materializes_ids_before_refetch_and_honours_eligible_limit(self):
        rows = [self.session() for _ in range(3)]
        materialized = []

        def ids():
            for row in rows:
                materialized.append(row.id)
                yield row.id

        def get_row(*, id, tenant):
            self.assertEqual(materialized, [row.id for row in rows])
            self.assertEqual(tenant, self.tenant)
            return next(row for row in rows if row.id == id)

        qs = self.session_filter.return_value.order_by.return_value
        qs.values_list.return_value = ids()
        self.session_get.side_effect = get_row
        call_command("backfill_meditation_lessons", "--tenant", str(self.tenant.id), "--limit", "1", stdout=self.output)
        qs.values_list.assert_called_once_with("id", flat=True)
        qs.iterator.assert_not_called()
        self.session_get.assert_called_once_with(id=rows[0].id, tenant=self.tenant)
        self.completion.assert_called_once()
        self.assertIn("eligible=1", self.output.getvalue())

    def test_refetch_failure_is_logged_and_next_row_runs(self):
        first, second = self.session(), self.session()
        self.session_get.side_effect = [MeditationSession.DoesNotExist("PRIVATE_SENTINEL"), second]
        with self.assertLogs(backfill.logger, level="WARNING") as logs:
            output = self.run_command([first, second])
        self.assertIn("scanned=2 written=1 skipped=0 failed=1 eligible=1", output)
        self.assertIn("error=DoesNotExist", logs.output[0])
        self.assertNotIn("PRIVATE_SENTINEL", logs.output[0])
        self.completion.assert_called_once()
        second.save.assert_called_once()

    def test_manifest_fallback_ignores_non_speech_and_limit_counts_eligible_rows(self):
        empty = self.session(guidance_text="", manifest={})
        fallback = self.session(
            guidance_text="  ",
            manifest={
                "phases": [
                    {
                        "segments": [
                            {"type": "speech", "text": "First speech."},
                            {"type": "silence", "text": "NOT_NARRATION"},
                            {"type": "speech", "text": "Second speech."},
                        ]
                    }
                ]
            },
        )
        excess = self.session()
        output = self.run_command([empty, fallback, excess], "--limit", "1")
        self.completion.assert_called_once()
        prompt = self.completion.call_args.args[1][1]["content"]
        self.assertIn("First speech.\n\nSecond speech.", prompt)
        self.assertNotIn("NOT_NARRATION", prompt)
        excess.save.assert_not_called()
        self.assertIn("scanned=2 written=1 skipped=1 failed=0", output)

    def test_guidance_takes_precedence_over_manifest(self):
        session = self.session(manifest={"phases": [{"segments": [{"type": "speech", "text": "NOT_USED"}]}]})
        self.run_command([session])
        prompt = self.completion.call_args.args[1][1]["content"]
        self.assertIn(session.guidance_text, prompt)
        self.assertNotIn("NOT_USED", prompt)

    def test_histogram_counts_successful_writes_only(self):
        self.completion.side_effect = [
            answer(),
            answer(tradition="zen"),
            answer(),
            answer(practice=""),
        ]
        output = self.run_command([self.session() for _ in range(4)])
        self.assertIn("scanned=4 written=3 skipped=0 failed=0 eligible=4 skipped_unsupported=1", output)
        self.assertEqual(self.completion.call_count, 4)
        self.assertEqual(
            output.splitlines()[1],
            f"backfill_meditation_lessons tenant={self.tenant.id} traditions "
            + " ".join(f"{t}={ {'taoist': 2, 'zen': 1}.get(t, 0) }" for t in TRADITIONS),
        )

    def test_all_iterates_core_enabled_tenants_and_applies_limit_per_tenant(self):
        other = Tenant(core_enabled=True)
        self.tenant_filter.return_value.order_by.return_value = [self.tenant, other]
        self.session_filter.return_value.order_by.return_value.values_list.side_effect = [
            self.candidate_ids([self.session(), self.session()]),
            self.candidate_ids([self.session(), self.session()]),
        ]
        call_command("backfill_meditation_lessons", "--all", "--limit", "1", stdout=self.output)
        self.tenant_filter.assert_called_once_with(core_enabled=True)
        self.tenant_get.assert_not_called()
        self.assertEqual([call.kwargs["tenant"] for call in self.session_filter.call_args_list], [self.tenant, other])
        self.assertEqual(self.completion.call_count, 2)
        for tenant in (self.tenant, other):
            self.assertIn(f"tenant={tenant.id} scanned=1 written=1 skipped=0 failed=0", self.output.getvalue())
        self.assertIn("tenants_failed=0", self.output.getvalue())

    def test_all_continues_after_tenant_selection_raises(self):
        other = Tenant(core_enabled=True)
        session = self.session(tenant=other)
        self.tenant_filter.return_value.order_by.return_value = [self.tenant, other]
        self.session_filter.return_value.order_by.return_value.values_list.side_effect = [
            RuntimeError("PRIVATE_SENTINEL"),
            self.candidate_ids([session]),
        ]
        with self.assertLogs(backfill.logger, level="WARNING") as logs:
            call_command("backfill_meditation_lessons", "--all", stdout=self.output)
        self.assertEqual([call.kwargs["tenant"] for call in self.session_filter.call_args_list], [self.tenant, other])
        self.completion.assert_called_once()
        session.save.assert_called_once()
        self.assertIn(f"tenant={other.id} scanned=1 written=1", self.output.getvalue())
        self.assertIn("tenants_failed=1", self.output.getvalue())
        self.assertIn("error=RuntimeError", logs.output[0])
        self.assertNotIn("PRIVATE_SENTINEL", logs.output[0])
        self.assertNotIn("PRIVATE_SENTINEL", self.output.getvalue())

    def test_scope_and_positive_limit_are_required(self):
        for args in (
            (),
            ("--tenant", str(self.tenant.id), "--all"),
            ("--tenant", "invalid"),
            ("--all", "--limit", "0"),
        ):
            with self.subTest(args=args), self.assertRaises(CommandError):
                call_command("backfill_meditation_lessons", *args, stdout=self.output)
        self.completion.assert_not_called()

    def test_unknown_tenant_errors(self):
        self.tenant_get.side_effect = Tenant.DoesNotExist
        with self.assertRaisesRegex(CommandError, "not found"):
            self.run_command([])
