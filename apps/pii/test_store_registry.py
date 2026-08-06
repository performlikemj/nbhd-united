from django.test import SimpleTestCase

from apps.pii.store_registry import registered_stores


class StoreRegistryTests(SimpleTestCase):
    def test_task_and_goal_flat_surfaces_are_registered(self):
        stores = {store.model_label: store for store in registered_stores()}
        self.assertEqual(set(stores), {"journal.Task", "journal.Goal"})
        for store in stores.values():
            self.assertEqual(store.flat_fields, ("title", "description"))
            self.assertEqual(store.json_paths, ())
            self.assertEqual(store.receipts_field, "pii_receipts")
