"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

from django.test import SimpleTestCase
from pgvector.django import CosineDistance, VectorField


class PgvectorSdkContractTest(SimpleTestCase):
    def test_vector_field_and_cosine_distance_accept_our_shapes(self):
        field = VectorField(dimensions=1536)
        distance = CosineDistance("embedding", [0.0] * 1536)

        self.assertEqual(field.dimensions, 1536)
        self.assertEqual(distance.arg_joiner, " <=> ")
        self.assertEqual(len(distance.get_source_expressions()), 2)
