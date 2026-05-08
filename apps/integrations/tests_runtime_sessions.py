"""Tests for the YardTalk session distillation runtime endpoints.

Covers:
- RuntimeSessionsPendingView (list undistilled non-test sessions)
- RuntimeSessionMarkProcessedView (mark distilled, idempotent)
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.test import TestCase
from django.test.utils import override_settings

from apps.journal.session_models import Session
from apps.tenants.services import create_tenant


def _make_session(
    tenant,
    *,
    project="proj",
    session_start=None,
    test_mode=False,
    processed_at=None,
    processed_summary=None,
    summary="A session.",
):
    start = session_start or datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    end = start.replace(hour=start.hour + 1)
    return Session.objects.create(
        tenant=tenant,
        source="yardtalk-mac/0.1.0",
        project=project,
        session_start=start,
        session_end=end,
        summary=summary,
        test_mode=test_mode,
        processed_at=processed_at,
        processed_summary=processed_summary or {},
    )


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeSessionsPendingViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Pending Tenant", telegram_chat_id=911001)
        self.other_tenant = create_tenant(display_name="Other Tenant", telegram_chat_id=911002)

    def _url(self, tenant_id=None):
        tid = tenant_id or self.tenant.id
        return f"/api/v1/integrations/runtime/{tid}/sessions/pending/"

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": str(tenant_id or self.tenant.id),
        }

    def test_returns_only_unprocessed_non_test_sessions(self):
        pending = _make_session(self.tenant, project="pending-one")
        _make_session(
            self.tenant,
            project="processed-one",
            processed_at=datetime(2026, 5, 7, 13, 0, tzinfo=UTC),
            processed_summary={"notes": "filed"},
        )
        _make_session(self.tenant, project="test-one", test_mode=True)

        response = self.client.get(self._url(), **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["sessions"][0]["id"], str(pending.id))
        self.assertEqual(body["sessions"][0]["project"], "pending-one")

    def test_orders_by_session_start_desc(self):
        older = _make_session(
            self.tenant,
            project="older",
            session_start=datetime(2026, 5, 6, 9, 0, tzinfo=UTC),
        )
        newer = _make_session(
            self.tenant,
            project="newer",
            session_start=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),
        )

        response = self.client.get(self._url(), **self._headers())
        body = response.json()
        ids = [s["id"] for s in body["sessions"]]
        self.assertEqual(ids, [str(newer.id), str(older.id)])

    def test_respects_limit_param(self):
        for i in range(5):
            _make_session(
                self.tenant,
                project=f"p{i}",
                session_start=datetime(2026, 5, 7, 9 + i, 0, tzinfo=UTC),
            )

        response = self.client.get(self._url(), {"limit": 2}, **self._headers())
        body = response.json()
        self.assertEqual(body["count"], 2)

    def test_caps_limit_at_25(self):
        for i in range(30):
            _make_session(
                self.tenant,
                project=f"p{i}",
                session_start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC).replace(
                    minute=i % 60
                ),
            )

        response = self.client.get(self._url(), {"limit": 999}, **self._headers())
        body = response.json()
        self.assertEqual(body["count"], 25)

    def test_invalid_limit_returns_400(self):
        response = self.client.get(self._url(), {"limit": "abc"}, **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_other_tenant_sessions_not_visible(self):
        _make_session(self.other_tenant, project="other-tenant-session")

        response = self.client.get(self._url(), **self._headers())
        body = response.json()
        self.assertEqual(body["count"], 0)

    def test_requires_internal_auth(self):
        _make_session(self.tenant)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 401)

    def test_returns_full_payload_fields(self):
        session = _make_session(self.tenant, project="full-fields")
        session.accomplishments = ["did a thing"]
        session.next_steps = ["do another thing"]
        session.references = {"clip_ids": ["abc"]}
        session.save()

        response = self.client.get(self._url(), **self._headers())
        body = response.json()
        s = body["sessions"][0]
        self.assertEqual(s["accomplishments"], ["did a thing"])
        self.assertEqual(s["next_steps"], ["do another thing"])
        self.assertEqual(s["references"], {"clip_ids": ["abc"]})
        # All distillation-relevant keys present
        for key in (
            "id",
            "summary",
            "blockers",
            "session_start",
            "session_end",
            "project",
            "project_identity",
            "project_type",
            "source",
        ):
            self.assertIn(key, s)


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeSessionMarkProcessedViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Mark Tenant", telegram_chat_id=911003)
        self.other_tenant = create_tenant(display_name="Other Tenant", telegram_chat_id=911004)
        self.session = _make_session(self.tenant)

    def _url(self, session_id=None, tenant_id=None):
        tid = tenant_id or self.tenant.id
        sid = session_id or self.session.id
        return f"/api/v1/integrations/runtime/{tid}/sessions/{sid}/mark-processed/"

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": str(tenant_id or self.tenant.id),
        }

    def test_marks_session_processed_and_stores_summary(self):
        summary = {"daily_note_date": "2026-05-07", "tasks_added": ["t1"], "memory_updated": True}
        response = self.client.post(
            self._url(),
            data=summary and {"processed_summary": summary},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session_id"], str(self.session.id))
        self.assertFalse(body["already_processed"])
        self.assertEqual(body["processed_summary"], summary)

        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.processed_at)
        self.assertEqual(self.session.processed_summary, summary)

    def test_idempotent_when_already_processed(self):
        first = self.client.post(
            self._url(),
            data={"processed_summary": {"first": True}},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(first.status_code, 200)
        first_processed_at = first.json()["processed_at"]

        second = self.client.post(
            self._url(),
            data={"processed_summary": {"second": True}},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertTrue(body["already_processed"])
        # Original processed_at is preserved; summary is NOT overwritten.
        self.assertEqual(body["processed_at"], first_processed_at)
        self.assertEqual(body["processed_summary"], {"first": True})

    def test_omitted_summary_defaults_to_empty_dict(self):
        response = self.client.post(
            self._url(),
            data={},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["processed_summary"], {})

    def test_invalid_summary_type_returns_400(self):
        response = self.client.post(
            self._url(),
            data={"processed_summary": "not a dict"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)

    def test_session_not_found_returns_404(self):
        bogus_id = "00000000-0000-0000-0000-000000000000"
        response = self.client.post(
            self._url(session_id=bogus_id),
            data={},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 404)

    def test_other_tenant_session_returns_404(self):
        # A session that belongs to other_tenant: caller hits their own
        # tenant URL but with that session_id — should 404, never reveal it.
        other_session = _make_session(self.other_tenant)
        response = self.client.post(
            self._url(session_id=other_session.id),
            data={},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 404)

    def test_requires_internal_auth(self):
        response = self.client.post(
            self._url(),
            data={},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
