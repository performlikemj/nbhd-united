from django.test import SimpleTestCase

from .catalog_annotation import annotate_incoming, incoming_name_paths, reinsert_catalog_refs


class CatalogAnnotationUnitTests(SimpleTestCase):
    def test_idempotent_ref_preserves_original_metadata_for_same_slug(self):
        payload = {
            "detail_json": {
                "exercises": [
                    {
                        "name": "Hammer Curl",
                        "catalog_ref": {"slug": "hammer-curl", "version": 1, "matched_by": "equipment_prefix"},
                    }
                ]
            }
        }
        paths = incoming_name_paths(payload)

        first, matches, unmatched = annotate_incoming(payload, paths)
        second, second_matches, second_unmatched = annotate_incoming(first, paths)

        self.assertEqual(first, second)
        self.assertEqual(matches, second_matches)
        self.assertEqual(unmatched, second_unmatched)
        self.assertEqual(
            first["detail_json"]["exercises"][0]["catalog_ref"],
            {"slug": "hammer-curl", "version": 1, "matched_by": "equipment_prefix"},
        )

    def test_user_verbatim_unresolved_is_not_reported(self):
        payload = {"skills": [{"name": "My private rehab move", "user_verbatim": True}]}
        annotated, matches, unmatched = annotate_incoming(payload, incoming_name_paths(payload))
        self.assertEqual(annotated, payload)
        self.assertEqual(matches, [])
        self.assertEqual(unmatched, [])

    def test_reinsert_restores_only_existing_server_refs(self):
        server_owned = {"exercises": [{"name": "Arnold Press", "catalog_ref": {"slug": "arnold-press", "version": 2}}]}
        authored = {
            "exercises": [{"name": "[PERSON_1] Press", "catalog_ref": {"slug": "[PERSON_2]-press", "version": 2}}]
        }
        self.assertEqual(
            reinsert_catalog_refs(authored, server_owned)["exercises"][0]["catalog_ref"]["slug"],
            "arnold-press",
        )
