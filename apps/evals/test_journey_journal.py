"""Tests for the Wave B Probe 2 suite — journal write→search journey canary.

These drive the REAL runtime endpoints (RuntimeDocumentView write + Postgres-FTS
RuntimeJournalSearchView read) in-process via the Django test client, mirroring
apps/integrations/tests_journal_search.py. The production suite uses httpx to the
same endpoints; here we inject an in-process transport so the *suite logic* under
test is the same code path that runs in prod.

The point of these tests is anti-green-theater: prove the suite PASSES only when a
written+committed marker doc is actually found through FTS, and FAILS (loudly, not
vacuously) when it is not.
"""

from __future__ import annotations

import json
import secrets

from django.test import TestCase, override_settings

from apps.evals.journey.targets import JourneyConfigError
from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import _assert_details_safe
from apps.evals.suites.journey_journal import CASE_ID, SUITE, run_journal_search_suite
from apps.evals.tasks import eval_journey_journal_task
from apps.journal.models import Document
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key

_INTERNAL_KEY = "shared-key"


def _synthetic_tenant() -> Tenant:
    """A synthetic, ACTIVE tenant carrying the per-tenant internal key.

    ``seed_internal_key`` stamps ``settings.NBHD_INTERNAL_API_KEY`` (set via the
    class-level override) so the transport's ``X-NBHD-Internal-Key`` header
    authenticates against the runtime endpoints.
    """
    email = f"{secrets.token_hex(4)}@e.com"
    user = User.objects.create_user(username=email, email=email)
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        is_synthetic=True,
        is_eval_sink=True,
    )
    return seed_internal_key(tenant)


class _DjangoClientTransport:
    """Drives the REAL runtime endpoints in-process via the Django test client.

    Identical endpoints/headers to the production httpx transport. ``drop_writes``
    simulates a broken write path — the PUT reports success but nothing persists —
    so the real FTS search legitimately returns zero results.
    """

    def __init__(self, client, *, drop_writes: bool = False) -> None:
        self._client = client
        self._drop_writes = drop_writes

    @staticmethod
    def _headers(tenant_id, internal_key):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": internal_key,
            "HTTP_X_NBHD_TENANT_ID": str(tenant_id),
        }

    def put_document(self, tenant_id, *, kind, slug, title, markdown, internal_key):
        if self._drop_writes:
            return 200, {}  # success-looking response, but nothing is written
        resp = self._client.put(
            f"/api/v1/integrations/runtime/{tenant_id}/document/",
            data=json.dumps({"kind": kind, "slug": slug, "title": title, "markdown": markdown}),
            content_type="application/json",
            **self._headers(tenant_id, internal_key),
        )
        return resp.status_code, (resp.json() if resp.status_code < 500 else {})

    def search(self, tenant_id, *, q, internal_key):
        resp = self._client.get(
            f"/api/v1/integrations/runtime/{tenant_id}/journal/search/",
            {"q": q},
            **self._headers(tenant_id, internal_key),
        )
        return resp.status_code, (resp.json() if resp.status_code < 500 else {})


class _SpyTransport:
    """Wraps an inner transport and records the args each call received."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.put_calls: list[dict] = []
        self.search_calls: list[dict] = []

    def put_document(self, tenant_id, **kwargs):
        self.put_calls.append(kwargs)
        return self._inner.put_document(tenant_id, **kwargs)

    def search(self, tenant_id, **kwargs):
        self.search_calls.append(kwargs)
        return self._inner.search(tenant_id, **kwargs)


@override_settings(NBHD_INTERNAL_API_KEY=_INTERNAL_KEY)
class JournalSearchSuiteTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()

    def _run(self, transport):
        with override_settings(EVAL_JOURNEY_TENANT_ID=str(self.tenant.id)):
            return run_journal_search_suite(transport=transport)

    def test_written_and_committed_doc_is_found(self):
        run = self._run(_DjangoClientTransport(self.client))

        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(run.suite, SUITE)
        results = list(run.results.all())
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.case_id, CASE_ID)
        self.assertEqual(result.kind, EvalResult.Kind.JOURNEY)
        self.assertTrue(result.passed)
        self.assertGreaterEqual(result.details["result_count"], 1)
        self.assertTrue(result.details["self_match"])
        # The probe deletes its own doc — no synthetic rows accumulate.
        self.assertEqual(Document.objects.filter(tenant=self.tenant).count(), 0)

    def test_missing_doc_makes_run_fail_not_vacuous(self):
        # Write path is a no-op → the REAL FTS search returns count=0 → the suite
        # must FAIL (a recorded, failing case), never PASS and never vacuously
        # ERROR-with-no-cases.
        run = self._run(_DjangoClientTransport(self.client, drop_writes=True))

        self.assertEqual(run.status, EvalRun.Status.FAIL)
        results = list(run.results.all())
        self.assertEqual(len(results), 1)  # a case WAS recorded — not vacuous
        self.assertFalse(results[0].passed)
        self.assertEqual(results[0].details["result_count"], 0)

    def test_search_uses_content_token_through_fts_endpoint_not_pk(self):
        spy = _SpyTransport(_DjangoClientTransport(self.client))
        run = self._run(spy)

        self.assertEqual(run.status, EvalRun.Status.PASS)
        self.assertEqual(len(spy.search_calls), 1)
        q = spy.search_calls[0]["q"]
        put = spy.put_calls[0]
        # The search term is a CONTENT token living inside the document body —
        # this is FTS over markdown, not a PK/slug lookup.
        self.assertIn(q, put["markdown"])
        self.assertNotEqual(q, put["slug"])
        self.assertNotEqual(q, put["markdown"])

    def test_details_are_metadata_only(self):
        run = self._run(_DjangoClientTransport(self.client))
        details = run.results.get().details
        # record() enforces this at the chokepoint; assert it directly too.
        _assert_details_safe(details)
        self.assertEqual(
            set(details),
            {"put_status", "search_status", "result_count", "self_match", "put_ms", "search_ms"},
        )
        for value in details.values():
            self.assertIsInstance(value, (int, bool))

    def test_misconfigured_target_closes_error_run(self):
        # An unresolvable target must produce a loud ERROR run (INVARIANT #3),
        # never a silent pass.
        with override_settings(EVAL_JOURNEY_TENANT_ID=""), self.assertRaises(JourneyConfigError):
            run_journal_search_suite(transport=_DjangoClientTransport(self.client))
        self.assertTrue(EvalRun.objects.filter(suite=SUITE, status=EvalRun.Status.ERROR).exists())


@override_settings(NBHD_INTERNAL_API_KEY=_INTERNAL_KEY)
class EvalJourneyJournalTaskTest(TestCase):
    def setUp(self):
        self.tenant = _synthetic_tenant()

    @override_settings(PLATFORM_OWNER_EMAIL="owner@test.com")
    def test_task_pass_path_no_alert(self):
        from django.core import mail

        with override_settings(EVAL_JOURNEY_TENANT_ID=str(self.tenant.id)):
            result = eval_journey_journal_task(transport=_DjangoClientTransport(self.client))

        self.assertEqual(result["status"], EvalRun.Status.PASS)
        self.assertEqual(result["suite"], SUITE)
        self.assertEqual(result["cases"], 1)
        self.assertEqual(len(mail.outbox), 0)  # no alert on a pass

    @override_settings(PLATFORM_OWNER_EMAIL="owner@test.com")
    def test_task_fail_path_alerts_and_raises(self):
        from django.core import mail

        with override_settings(EVAL_JOURNEY_TENANT_ID=str(self.tenant.id)), self.assertRaises(RuntimeError):
            eval_journey_journal_task(transport=_DjangoClientTransport(self.client, drop_writes=True))

        # Owner alerted before the DLQ-raise (best-effort, content-free).
        self.assertEqual(len(mail.outbox), 1)
