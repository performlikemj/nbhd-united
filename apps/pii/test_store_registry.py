from django.test import SimpleTestCase

from apps.pii.store_registry import registered_stores


class StoreRegistryTests(SimpleTestCase):
    def test_task_and_goal_flat_surfaces_are_registered(self):
        stores = {store.model_label: store for store in registered_stores()}
        self.assertEqual(
            set(stores),
            {"journal.Task", "journal.Goal", "journal.PendingTaskAction"},
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
