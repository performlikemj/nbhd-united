"""Journal retrieval-coordinate coverage for cold iOS thread recaps."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.router.models import AppChatMessage, ChatThread
from apps.router.thread_recap import RECAP_TOTAL_CHAR_CAP, build_thread_recap_block
from apps.tenants.models import Tenant, User


class ThreadRecapJournalReferenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="recap-artifact", password="x")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            pii_entity_map={"[PERSON_1]": {"name": "Sarah"}},
        )
        self.thread = ChatThread.objects.create(
            tenant=self.tenant,
            user=self.user,
            title="Artifact thread",
            is_main=True,
        )

    def _prior_turn(self, *, client_id: str, reply_text: str, journal_link=None):
        row = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id=client_id,
            user_text="Discuss the report",
            reply_text=reply_text,
            journal_link=journal_link,
            status=AppChatMessage.Status.READY,
            replied_at=timezone.now() - timedelta(hours=2),
        )
        self.tenant.last_wake_at = timezone.now() - timedelta(hours=1)
        return row

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    def test_cold_recap_has_exact_placeholder_space_coordinate(self, _detect):
        self._prior_turn(
            client_id="artifact-turn",
            reply_text="Saved the full table.",
            journal_link={
                "kind": "project",
                "slug": "assistant-table-20260715-deadbeef",
                "title": "Table for [PERSON_1]",
            },
        )

        recap = build_thread_recap_block(self.tenant, str(self.thread.id))

        self.assertIn(
            "[journal-ref: project|assistant-table-20260715-deadbeef|Table for [PERSON_1]; "
            "retrieve with nbhd_document_get]",
            recap,
        )
        self.assertNotIn("Sarah", recap)
        self.assertLessEqual(len(recap), RECAP_TOTAL_CHAR_CAP)

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    def test_reference_does_not_break_total_cap(self, _detect):
        for index in range(8):
            self._prior_turn(
                client_id=f"artifact-{index}",
                reply_text=(f"reply-{index} " + ("word " * 100)),
                journal_link={
                    "kind": "project",
                    "slug": f"assistant-table-{index}",
                    "title": "T" * 80,
                },
            )
        recap = build_thread_recap_block(self.tenant, str(self.thread.id))
        self.assertLessEqual(len(recap), RECAP_TOTAL_CHAR_CAP)
        self.assertIn("journal-ref", recap)
