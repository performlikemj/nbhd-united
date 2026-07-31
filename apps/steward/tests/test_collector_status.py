from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.steward.collectors import status
from apps.steward.models import CollectorStatus


class CollectorStatusTests(TestCase):
    def test_postgres_persistence_waits_are_bounded_per_transaction(self):
        raw_cursor = MagicMock()
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = raw_cursor
        cursor_context.__exit__.return_value = False

        with patch.object(status.connection, "cursor", return_value=cursor_context):
            status.set_persistence_timeouts()

        self.assertEqual(
            [call.args[0] for call in raw_cursor.execute.call_args_list],
            [
                "SET LOCAL lock_timeout = '5s'",
                "SET LOCAL statement_timeout = '30s'",
            ],
        )

    def test_stale_lease_self_expires_and_release_matches_claim(self):
        now = timezone.now()
        CollectorStatus.objects.create(
            collector=CollectorStatus.Collector.ASC,
            held_until=now - timedelta(seconds=1),
        )

        held_until = status.acquire_collector_lease(
            CollectorStatus.Collector.ASC,
            now=now,
        )

        self.assertEqual(held_until, now + timedelta(minutes=10))
        status.release_collector_lease(
            CollectorStatus.Collector.ASC,
            held_until,
        )
        self.assertIsNone(
            CollectorStatus.objects.get(
                collector=CollectorStatus.Collector.ASC,
            ).held_until
        )
