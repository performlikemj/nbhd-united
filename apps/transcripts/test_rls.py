from __future__ import annotations

import contextlib
import uuid

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from apps.tenants.management.commands.disable_rls import RLS_KEEP_ENABLED
from apps.tenants.models import Tenant, User

from .models import TranscriptEvent

_TRANSCRIPT_TABLES = {
    "transcripts_transcriptevent",
    "transcripts_transcriptcapturequarantine",
    "transcripts_transcriptindexoutbox",
}


class TranscriptRlsBackstopTest(TestCase):
    def setUp(self):
        owner_user = User.objects.create_user(username="transcript-rls-owner", password="pass")
        other_user = User.objects.create_user(username="transcript-rls-other", password="pass")
        self.owner = Tenant.objects.create(user=owner_user, status=Tenant.Status.ACTIVE)
        self.other = Tenant.objects.create(user=other_user, status=Tenant.Status.ACTIVE)
        self.event = TranscriptEvent.objects.create(
            tenant=self.owner,
            turn_id=uuid.uuid4(),
            role=TranscriptEvent.Role.USER,
            source_type=TranscriptEvent.SourceType.IOS_QUEUED,
            source_event_id="rls-event",
            channel=TranscriptEvent.Channel.IOS,
            occurred_at=timezone.now(),
            text_enc=b"",
            content_hash="0" * 64,
        )

    @contextlib.contextmanager
    def _app_user(self, *, tenant_id=None, service_role=False):
        cur = connection.cursor()
        try:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cur.execute("GRANT USAGE ON SCHEMA public TO app_user")
            cur.execute("GRANT SELECT ON transcripts_transcriptevent TO app_user")
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", [str(tenant_id) if tenant_id else ""])
            cur.execute("SELECT set_config('app.service_role', %s, false)", ["true" if service_role else ""])
            cur.execute("SET ROLE app_user")
            yield cur
        finally:
            cur.execute("RESET ROLE")
            cur.execute("SELECT set_config('app.tenant_id', '', false), set_config('app.service_role', '', false)")

    @staticmethod
    def _visible_ids(cur):
        cur.execute("SELECT id FROM transcripts_transcriptevent")
        return {row[0] for row in cur.fetchall()}

    def test_policy_fails_closed_and_allows_tenant_or_service_context(self):
        with self._app_user() as cur:
            self.assertEqual(self._visible_ids(cur), set())
        with self._app_user(tenant_id=self.other.id) as cur:
            self.assertEqual(self._visible_ids(cur), set())
        with self._app_user(tenant_id=self.owner.id) as cur:
            self.assertEqual(self._visible_ids(cur), {self.event.id})
        with self._app_user(service_role=True) as cur:
            self.assertEqual(self._visible_ids(cur), {self.event.id})

    def test_all_transcript_tables_keep_rls_at_boot(self):
        self.assertTrue(_TRANSCRIPT_TABLES.issubset(RLS_KEEP_ENABLED))

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT class.relname, class.relrowsecurity, class.relforcerowsecurity
                FROM pg_class AS class
                JOIN pg_namespace AS namespace ON namespace.oid = class.relnamespace
                WHERE namespace.nspname = 'public' AND class.relname = ANY(%s)
                """,
                [list(_TRANSCRIPT_TABLES)],
            )
            states = {row[0]: row[1:] for row in cur.fetchall()}
        self.assertEqual(set(states), _TRANSCRIPT_TABLES)
        self.assertTrue(all(enabled and forced for enabled, forced in states.values()))
