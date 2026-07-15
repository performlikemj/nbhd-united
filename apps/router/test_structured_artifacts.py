"""Detection and Journal externalization tests for oversized GFM tables."""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.journal.models import Document
from apps.router.structured_artifacts import (
    ArtifactThresholds,
    find_gfm_tables,
    replace_selected_tables,
    select_large_tables,
)
from apps.tenants.models import Tenant, User


def _table(rows: int, *, leading: bool = True, cell: str = "value") -> str:
    if leading:
        lines = ["| Name | Value |", "| --- | ---: |"]
        lines.extend(f"| row {index} | {cell} |" for index in range(rows))
    else:
        lines = ["Name | Value", "--- | ---:"]
        lines.extend(f"row {index} | {cell}" for index in range(rows))
    return "\n".join(lines)


class GfmTableDetectionTests(SimpleTestCase):
    def test_leading_trailing_and_no_leading_pipe_forms(self):
        for text in (_table(2), _table(2, leading=False)):
            with self.subTest(text=text.splitlines()[0]):
                tables = find_gfm_tables(text)
                self.assertEqual(len(tables), 1)
                self.assertEqual(tables[0].row_count, 2)

    def test_exactly_25_rows_stays_inline_and_26_moves(self):
        twenty_five = find_gfm_tables(_table(25))
        twenty_six = find_gfm_tables(_table(26))
        self.assertEqual(select_large_tables(twenty_five), [])
        self.assertEqual(select_large_tables(twenty_six), twenty_six)

    def test_wide_table_over_6000_chars_moves(self):
        tables = find_gfm_tables(_table(1, cell="x" * 6100))
        self.assertGreater(tables[0].char_count, 6000)
        self.assertEqual(select_large_tables(tables), tables)

    def test_aggregate_threshold_moves_all_tables(self):
        tables = find_gfm_tables(_table(21) + "\n\n" + _table(20, leading=False))
        self.assertEqual([table.row_count for table in tables], [21, 20])
        self.assertEqual(select_large_tables(tables), tables)

    def test_escaped_pipes_and_inline_code_do_not_split_cells(self):
        text = "| A | B |\n| --- | --- |\n| left \\| right | `x | y` |"
        tables = find_gfm_tables(text)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].row_count, 1)

    def test_gfm_body_rows_with_missing_or_extra_cells_remain_in_table(self):
        text = "| A | B |\n| --- | --- |\n| one |\n| two | three | extra |"
        tables = find_gfm_tables(text)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].row_count, 2)

    def test_backtick_and_tilde_fenced_tables_are_ignored(self):
        text = f"```md\n{_table(30)}\n```\n\n~~~\n{_table(30)}\n~~~"
        self.assertEqual(find_gfm_tables(text), [])

    def test_four_space_and_tab_indented_tables_are_ignored(self):
        spaced = "\n".join(f"    {line}" for line in _table(30).splitlines())
        tabbed = "\n".join(f"\t{line}" for line in _table(30).splitlines())
        self.assertEqual(find_gfm_tables(spaced), [])
        self.assertEqual(find_gfm_tables(tabbed), [])

    def test_malformed_delimiter_is_ignored(self):
        text = "| A | B |\n| -- | nope |\n| 1 | 2 |"
        self.assertEqual(find_gfm_tables(text), [])

    def test_table_only_replacement_keeps_header_and_first_three_rows(self):
        text = _table(26)
        selected = select_large_tables(find_gfm_tables(text))
        replaced = replace_selected_tables(text, selected, "Table from chat")
        self.assertEqual(
            replaced,
            "| Name | Value |\n"
            "| --- | ---: |\n"
            "| row 0 | value |\n"
            "| row 1 | value |\n"
            "| row 2 | value |\n"
            "Saved the full table (26 rows) to your Journal as “Table from chat”.",
        )

    def test_under_threshold_table_is_untouched(self):
        text = "Before.\n\n" + _table(25) + "\n\nAfter."
        selected = select_large_tables(find_gfm_tables(text))
        self.assertEqual(replace_selected_tables(text, selected, "Table from chat"), text)

    def test_multi_table_replacement_previews_first_table_only(self):
        text = (
            "Before.\n\n"
            + _table(21, cell="first")
            + "\n\nBetween.\n\n"
            + _table(20, leading=False, cell="second")
            + "\n\nAfter."
        )
        selected = select_large_tables(find_gfm_tables(text))
        replaced = replace_selected_tables(text, selected, "Table — Report")
        self.assertIn("Before.", replaced)
        self.assertIn("Between.", replaced)
        self.assertIn("After.", replaced)
        self.assertEqual(replaced.count("Saved the full tables"), 1)
        self.assertIn("| Name | Value |", replaced)
        self.assertIn("| row 2 | first |", replaced)
        self.assertNotIn("| row 3 | first |", replaced)
        self.assertNotIn("row 0 | second", replaced)


class StructuredArtifactRolloutTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="artifact-detector", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        self.text = "# Quarterly Review\n\n" + _table(26)

    def _externalize(self, *, text=None, key="client-1", journal_link=None, defaults=None):
        from apps.router.structured_artifacts import externalize_large_structured_reply

        kwargs = {
            "tenant": self.tenant,
            "text": self.text if text is None else text,
            "source": "ios",
            "dedup_key": key,
            "journal_link": journal_link,
        }
        if defaults is not None:
            kwargs["defaults"] = defaults
        return externalize_large_structured_reply(
            **kwargs,
        )

    def test_flag_off_runs_dark_telemetry_without_moving(self):
        with self.assertLogs("apps.router.structured_artifacts", level="INFO") as logs:
            result = self._externalize()
        self.assertFalse(result.moved)
        self.assertEqual(result.failure_reason, "flag_disabled")
        self.assertEqual(result.stored_text, self.text)
        self.assertFalse(Document.objects.filter(tenant=self.tenant, slug__startswith="assistant-table-").exists())
        telemetry = " ".join(logs.output)
        self.assertIn("reply_artifact_telemetry", telemetry)
        self.assertIn("flag_state=False", telemetry)
        self.assertNotIn("row 1 |", telemetry)

    def test_flag_on_moves_complete_reply_and_reprocessing_stored_text_is_noop(self):
        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal"])

        first = self._externalize()
        dedup_retry = self._externalize()
        reprocessed = self._externalize(
            text=first.stored_text,
            journal_link=first.journal_link,
            defaults=ArtifactThresholds(individual_rows=2),
        )

        self.assertTrue(first.moved)
        self.assertEqual(first.document_id, dedup_retry.document_id)
        self.assertFalse(reprocessed.moved)
        self.assertEqual(reprocessed.stored_text, first.stored_text)
        self.assertIn("Saved the full table (26 rows)", first.stored_text)
        self.assertIn("| Name | Value |", first.stored_text)
        self.assertIn("| row 2 | value |", first.stored_text)
        self.assertNotIn("| row 3 | value |", first.stored_text)
        docs = Document.objects.filter(tenant=self.tenant, slug__startswith="assistant-table-")
        self.assertEqual(docs.count(), 1)
        doc = docs.get()
        self.assertEqual(doc.kind, Document.Kind.PROJECT)
        self.assertEqual(doc.title, "Table — Quarterly Review")
        self.assertIn("# Quarterly Review", doc.markdown)
        self.assertIn("| row 25 | value |", doc.markdown)
        self.assertEqual(first.journal_link, {"kind": "project", "slug": doc.slug, "title": doc.title})

    def test_related_existing_link_is_reused_without_duplicate(self):
        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal"])
        linked = Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.PROJECT,
            slug="already-saved",
            title="Existing table",
            markdown=self.text,
        )
        result = self._externalize(journal_link={"kind": "project", "slug": linked.slug, "title": linked.title})
        self.assertTrue(result.moved)
        self.assertEqual(result.document_id, str(linked.id))
        self.assertEqual(Document.objects.filter(tenant=self.tenant).count(), 1)

    def test_unrelated_link_loses_to_generated_artifact(self):
        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal"])
        linked = Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.PROJECT,
            slug="elsewhere",
            title="Elsewhere",
            markdown="Unrelated notes",
        )
        result = self._externalize(journal_link={"kind": "project", "slug": linked.slug, "title": linked.title})
        self.assertTrue(result.moved)
        self.assertNotEqual(result.journal_link["slug"], linked.slug)
        self.assertEqual(Document.objects.filter(tenant=self.tenant).count(), 2)

    def test_unrelated_primary_slug_collision_uses_secondary_hash(self):
        from apps.journal.reply_artifacts import _identity, _slug

        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal"])
        identity = _identity(tenant=self.tenant, source="ios", dedup_key="client-1")
        primary_slug = _slug(
            local_date=timezone.now().date(),
            identity=identity,
            attempt=0,
        )
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.PROJECT,
            slug=primary_slug,
            title="User document",
            markdown="Do not overwrite",
        )
        result = self._externalize()
        self.assertTrue(result.moved)
        self.assertNotEqual(result.journal_link["slug"], primary_slug)
        self.assertEqual(Document.objects.get(slug=primary_slug).markdown, "Do not overwrite")

    def test_all_deterministic_slug_collisions_fall_back_inline(self):
        from apps.journal.reply_artifacts import _MAX_SLUG_ATTEMPTS, _identity, _slug

        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal"])
        identity = _identity(tenant=self.tenant, source="ios", dedup_key="client-1")
        for attempt in range(_MAX_SLUG_ATTEMPTS):
            Document.objects.create(
                tenant=self.tenant,
                kind=Document.Kind.PROJECT,
                slug=_slug(local_date=timezone.now().date(), identity=identity, attempt=attempt),
                title=f"User document {attempt}",
                markdown=f"Never overwrite {attempt}",
            )

        result = self._externalize()

        self.assertFalse(result.moved)
        self.assertEqual(result.failure_reason, "journal_write_failed")
        self.assertEqual(result.stored_text, self.text)
        self.assertEqual(Document.objects.filter(tenant=self.tenant).count(), _MAX_SLUG_ATTEMPTS)
