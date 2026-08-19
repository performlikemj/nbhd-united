from django.db import models
from django.test import SimpleTestCase

from apps.pii.authoring import _registered_field_max_length
from apps.pii.store_registry import json_path_parts, registered_store, registered_stores, rewrite_json_path


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
                "lessons.Lesson",
                "lessons.StarJournalEntry",
                "fuel.WorkoutPlan",
                "fuel.Workout",
                "fuel.FuelProfile",
                "fuel.WorkoutTemplate",
                "fuel.SleepLog",
                "datebook.MirrorEvent",
                "datebook.MirrorReminder",
                "datebook.DeviceCommand",
                "datebook.DatebookDestinationDefault",
                "finance.FinanceAccount",
                "finance.FinanceTransaction",
                "finance.PayoffPlan",
                "finance.FinanceSnapshot",
                "actions.PendingAction",
                "actions.ActionAuditLog",
                "journal.Session",
                "journal.NoteTemplate",
                "router.DeliveryAttempt",
                "core.CoreProfile",
                "core.MeditationSession",
                "integrations.SautaiMealPlanJob",
                "automations.AutomationRun",
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

    def test_registry_contract_is_model_valid_and_directly_tenant_scoped(self):
        stores = registered_stores()
        self.assertEqual(len(stores), len({store.model_label for store in stores}))

        for store in stores:
            with self.subTest(model_label=store.model_label):
                tenant_field = store.model._meta.get_field("tenant")
                self.assertTrue(tenant_field.is_relation)
                self.assertIsInstance(
                    store.model._meta.get_field(store.receipts_field),
                    models.JSONField,
                )
                for field in store.flat_fields:
                    self.assertIsInstance(
                        store.model._meta.get_field(field),
                        (models.CharField, models.TextField),
                    )
                for path in store.json_paths:
                    parts = json_path_parts(path)
                    self.assertTrue(parts)
                    self.assertIsInstance(store.model._meta.get_field(parts[0]), models.JSONField)
                    if "**" in parts:
                        self.assertEqual(parts[-1], "**")

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

    def test_w3b_long_tail_surfaces_are_registered_exactly(self):
        stores = {store.model_label: store for store in registered_stores()}
        expected = {
            "lessons.Lesson": (("text", "context", "galaxy_note"), ()),
            "lessons.StarJournalEntry": (("text",), ()),
            "fuel.WorkoutPlan": (
                ("name", "notes", "objective"),
                ("schedule_json.**", "week_overrides.**"),
            ),
            "fuel.Workout": (
                ("skip_reason", "activity", "notes"),
                ("notes_thread[].text", "detail_json.**"),
            ),
            "fuel.FuelProfile": (("additional_context",), ("limitations[]",)),
            "fuel.WorkoutTemplate": (("name",), ("detail_json.**",)),
            "fuel.SleepLog": (("notes",), ()),
            "datebook.MirrorEvent": (
                ("title", "location", "notes", "calendar_title", "source_title"),
                (
                    "staged_payload.title",
                    "staged_payload.location",
                    "staged_payload.notes",
                    "staged_payload.calendar_title",
                    "staged_payload.source_title",
                ),
            ),
            "datebook.MirrorReminder": (
                ("title", "location", "notes", "list_title", "source_title"),
                (
                    "staged_payload.title",
                    "staged_payload.location",
                    "staged_payload.notes",
                    "staged_payload.list_title",
                    "staged_payload.source_title",
                ),
            ),
            "datebook.DeviceCommand": (
                ("display_text", "destination_name", "result_display"),
                (
                    "payload.items[].title",
                    "payload.items[].location",
                    "payload.items[].notes",
                    "payload.items[].destination_name",
                    "payload.items[].calendar_title",
                    "payload.items[].list_title",
                ),
            ),
            "datebook.DatebookDestinationDefault": (("name",), ()),
            "finance.FinanceAccount": (("nickname",), ()),
            "finance.FinanceTransaction": (("description",), ()),
            "finance.PayoffPlan": ((), ("schedule_json.**",)),
            "finance.FinanceSnapshot": ((), ("accounts_json.**",)),
            "actions.PendingAction": (("display_summary",), ("action_payload.**",)),
            "actions.ActionAuditLog": (("display_summary",), ("action_payload.**",)),
            "journal.Session": (
                ("project", "summary"),
                ("accomplishments[]", "blockers[]", "next_steps[]", "processed_summary.**"),
            ),
            "journal.NoteTemplate": (("name",), ("sections[].title", "sections[].content")),
            "router.DeliveryAttempt": (("response_excerpt",), ()),
            "core.CoreProfile": (("additional_context",), ()),
            "core.MeditationSession": (
                ("title", "theme", "guidance_text", "feedback_note"),
                (
                    "manifest.title",
                    "manifest.theme",
                    "manifest.phases[].intent",
                    "manifest.phases[].segments[].text",
                ),
            ),
            "integrations.SautaiMealPlanJob": (("user_prompt",), ()),
            "automations.AutomationRun": ((), ("input_payload.**", "result_payload.**")),
        }

        for model_label, (flat_fields, json_paths) in expected.items():
            with self.subTest(model_label=model_label):
                self.assertEqual(stores[model_label].flat_fields, flat_fields)
                self.assertEqual(stores[model_label].json_paths, json_paths)
                self.assertEqual(stores[model_label].receipts_field, "pii_receipts")

    def test_recursive_json_path_walks_dicts_and_lists_copy_on_write(self):
        original = {
            "summary": "Alice",
            "nested": [1, {"coach": "Bob", "active": True}, ["Cara"]],
            "nothing": None,
        }

        rewritten, changed = rewrite_json_path(
            original,
            json_path_parts("**"),
            lambda value: f"<{value}>",
        )

        self.assertTrue(changed)
        self.assertEqual(
            rewritten,
            {
                "summary": "<Alice>",
                "nested": [1, {"coach": "<Bob>", "active": True}, ["<Cara>"]],
                "nothing": None,
            },
        )
        self.assertEqual(original["summary"], "Alice")
        self.assertEqual(original["nested"][1]["coach"], "Bob")
        self.assertEqual(original["nested"][2][0], "Cara")
        self.assertEqual(
            registered_store("automations.AutomationRun").nested_json_paths("input_payload"),
            (("**",),),
        )

    def test_recursive_json_path_must_be_terminal(self):
        with self.assertRaisesRegex(ValueError, r"\*\* must be the final JSON path component"):
            rewrite_json_path({"name": "Alice"}, ("**", "name"), str.upper)

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

    def test_registered_field_limit_requires_unambiguous_model_label(self):
        """Name-only lookup is forbidden even when today's namesakes agree."""
        with self.assertRaises(TypeError):
            _registered_field_max_length("nickname")  # type: ignore[call-arg]
        self.assertEqual(_registered_field_max_length("title", "journal.Task"), 256)
        self.assertEqual(_registered_field_max_length("title", "core.MeditationSession"), 160)
        self.assertIsNone(_registered_field_max_length("description", "journal.Task"))
        self.assertEqual(_registered_field_max_length("description", "finance.FinanceTransaction"), 256)
