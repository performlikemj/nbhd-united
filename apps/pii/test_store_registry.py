from django.test import SimpleTestCase

from apps.pii.authoring import _registered_field_max_length
from apps.pii.store_registry import registered_stores


class StoreRegistryTests(SimpleTestCase):
    def test_registered_surfaces(self):
        stores = {store.model_label: store for store in registered_stores()}
        self.assertEqual(
            set(stores),
            {
                "journal.Task",
                "journal.Goal",
                "journal.PendingTaskAction",
                "journal.Document",
                "journal.DocumentChunk",
                "journal.DocumentIngestion",
                "journal.DocumentIngestionArtifact",
                "journal.DailyNote",
                "journal.JournalEntry",
                "journal.WeeklyReview",
                "journal.Purpose",
                "journal.PendingExtraction",
            },
        )
        for model_label in ("journal.Task", "journal.Goal"):
            store = stores[model_label]
            self.assertEqual(store.flat_fields, ("title", "description"))
            self.assertEqual(store.json_paths, ())
            self.assertEqual(store.receipts_field, "pii_receipts")

        evidence_store = stores["journal.PendingTaskAction"]
        self.assertEqual(evidence_store.flat_fields, ("evidence",))
        self.assertEqual(evidence_store.json_paths, ())
        self.assertEqual(evidence_store.receipts_field, "pii_receipts")

    def test_document_family_flat_surfaces_are_registered(self):
        stores = {store.model_label: store for store in registered_stores()}
        self.assertEqual(stores["journal.Document"].flat_fields, ("title", "markdown"))
        self.assertEqual(stores["journal.DocumentChunk"].flat_fields, ("text",))
        self.assertEqual(stores["journal.DocumentIngestion"].flat_fields, ("original_filename",))
        self.assertEqual(stores["journal.DocumentIngestionArtifact"].flat_fields, ("content_excerpt",))
        for label in (
            "journal.Document",
            "journal.DocumentChunk",
            "journal.DocumentIngestion",
            "journal.DocumentIngestionArtifact",
        ):
            self.assertEqual(stores[label].json_paths, ())
            self.assertEqual(stores[label].receipts_field, "pii_receipts")

    def test_w3a_json_and_legacy_surfaces_are_registered(self):
        stores = {store.model_label: store for store in registered_stores()}
        self.assertEqual(stores["journal.DailyNote"].flat_fields, ("markdown",))
        self.assertEqual(
            stores["journal.JournalEntry"].json_paths,
            ("wins[]", "challenges[]"),
        )
        self.assertEqual(
            stores["journal.WeeklyReview"].json_paths,
            (
                "top_wins[]",
                "top_challenges[]",
                "lessons[]",
                "intentions_next_week[]",
            ),
        )
        self.assertEqual(stores["journal.Purpose"].json_paths, ("evidence[].note",))
        self.assertEqual(stores["journal.PendingExtraction"].flat_fields, ("text",))
        for label in (
            "journal.DailyNote",
            "journal.JournalEntry",
            "journal.WeeklyReview",
            "journal.Purpose",
            "journal.PendingExtraction",
        ):
            self.assertEqual(stores[label].receipts_field, "pii_receipts")

    def test_registered_field_max_length_is_resolved_per_model(self):
        """A store's limit comes from ITS column, not the strictest namesake."""
        self.assertEqual(_registered_field_max_length("title", "journal.Document"), 256)
        self.assertEqual(_registered_field_max_length("original_filename", "journal.DocumentIngestion"), 255)
        # A field a store does not register resolves to no limit for that store,
        # even though another store caps the same name.
        self.assertIsNone(_registered_field_max_length("title", "journal.DocumentChunk"))
        # TextField columns have no limit at all.
        self.assertIsNone(_registered_field_max_length("markdown", "journal.Document"))

    def test_flat_field_limits_are_unambiguous_by_name(self):
        """Guard for callers that omit ``model_label``.

        Without it, :func:`_registered_field_max_length` answers with the
        strictest limit across every store sharing the field NAME. That is exact
        only while those stores agree. If this fails, a registration has
        diverged — thread ``model_label`` through the call sites writing the
        roomier column instead of relaxing this test.
        """
        limits: dict[str, set[int | None]] = {}
        for store in registered_stores():
            for field in store.flat_fields:
                limits.setdefault(field, set()).add(getattr(store.model._meta.get_field(field), "max_length", None))
        ambiguous = {field: sorted(map(str, values)) for field, values in limits.items() if len(values) > 1}
        self.assertEqual(ambiguous, {})
