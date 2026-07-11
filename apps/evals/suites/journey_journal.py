"""Journey-canary Probe 2 — journal write→search (Wave B, docs/evals-wave-b-plan.md).

One module per journey probe (so the sibling probe PRs don't collide on a shared
file). This probe exercises the end-to-end journal journey through the *actual*
runtime endpoints an OpenClaw container hits — not a re-implementation — so a green
run means the product path works, not just that a process is up
(docs/evals-directive.md §0).

``run_journal_search_suite``: PUT a Document with a unique content marker through
the real ``RuntimeDocumentView`` path, then find it again through the real
Postgres-FTS ``RuntimeJournalSearchView`` by that marker. This canaries the
invariant that journal search is Postgres-side (never SQLite on the per-tenant
share).

INVARIANT #1 (docs/evals-directive.md): nothing content-bearing enters the eval
pipeline. The synthetic marker/slug are probe-generated, never real-user data, and
``record()`` only stores counts / statuses / durations / booleans. Result rows are
read for ``(kind, slug)`` metadata ONLY — never ``snippet``/``title``/``rank``,
which echo document content.
"""

from __future__ import annotations

import logging
import secrets
import string
import time

import httpx
from django.conf import settings

from apps.evals.journey.targets import JourneyConfigError, resolve_journey_tenant
from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import record, record_run
from apps.journal.models import Document

logger = logging.getLogger(__name__)

SUITE = "journey_journal"
CASE_ID = "journal_write_search"

# A non-daily kind so the slug isn't forced to be an ISO date, and one that is NOT
# promoted to a typed Goal/Task model — the probe doc stays a plain Document.
_KIND = Document.Kind.IDEAS.value
_TITLE = "eval journey journal probe"
_MARKDOWN_PREFIX = "eval journey journal probe marker"
_HTTP_TIMEOUT_S = 30.0


def _new_marker() -> str:
    """A unique, non-word content token to write into the doc and search for.

    Pure lowercase letters tokenize cleanly under the Postgres ``english`` FTS
    config (no stopword or number-lexeme edge cases), and the same stemmer is
    applied to both the stored ``to_tsvector`` and the ``websearch_to_tsquery``
    so an exact token always matches itself. Uniqueness per run is the load-bearing
    property: a stale doc left behind by a past run carries a *different* marker, so
    it can never read this run green (the "stale row is green forever" trap).
    """
    return "evalmarker" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(16))


class HttpxRuntimeTransport:
    """Production transport — real HTTP to the control plane's own runtime API.

    This is the same request an OpenClaw container makes: internal-key auth to
    ``/api/v1/integrations/runtime/<tenant>/…``. Each call is its own HTTP request,
    so the write commits in its own transaction before the search reads it (MVCC —
    the two are deliberately NOT wrapped in one ``atomic()``). Tests inject a
    transport that drives the identical endpoints in-process via the Django test
    client, so the suite logic under test is the same code that runs in prod.
    """

    def __init__(self, base_url: str | None = None, *, timeout: float = _HTTP_TIMEOUT_S) -> None:
        resolved = base_url or getattr(settings, "DJANGO_BASE_URL", "") or getattr(settings, "API_BASE_URL", "") or ""
        self._base_url = resolved.rstrip("/")
        self._timeout = timeout

    def _require_base(self) -> str:
        if not self._base_url:
            # A probe that can't reach its endpoints is broken, not a silent pass
            # (INVARIANT #3). Raised inside record_run → the run closes ``error``.
            raise JourneyConfigError(
                "Neither DJANGO_BASE_URL nor API_BASE_URL is set — cannot reach the runtime "
                "journal endpoints for the journey canary."
            )
        return self._base_url

    @staticmethod
    def _headers(tenant_id, internal_key: str) -> dict:
        return {"X-NBHD-Internal-Key": internal_key, "X-NBHD-Tenant-Id": str(tenant_id)}

    def put_document(self, tenant_id, *, kind, slug, title, markdown, internal_key) -> tuple[int, dict]:
        url = f"{self._require_base()}/api/v1/integrations/runtime/{tenant_id}/document/"
        resp = httpx.put(
            url,
            json={"kind": kind, "slug": slug, "title": title, "markdown": markdown},
            headers=self._headers(tenant_id, internal_key),
            timeout=self._timeout,
        )
        return resp.status_code, _json_dict(resp)

    def search(self, tenant_id, *, q, internal_key) -> tuple[int, dict]:
        url = f"{self._require_base()}/api/v1/integrations/runtime/{tenant_id}/journal/search/"
        resp = httpx.get(
            url,
            params={"q": q},
            headers=self._headers(tenant_id, internal_key),
            timeout=self._timeout,
        )
        return resp.status_code, _json_dict(resp)


def _json_dict(resp) -> dict:
    """Parse a JSON object body, or ``{}`` on a non-JSON / error response.

    A broken endpoint (500 HTML, empty body) yields ``{}`` → ``count`` reads 0 →
    the case fails. We never let a decode error masquerade as a pass.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — any non-JSON body is treated as "no results"
        return {}
    return body if isinstance(body, dict) else {}


def run_journal_search_suite(*, transport=None, trigger: str = EvalRun.Trigger.MANUAL) -> EvalRun:
    """Run Probe 2 (journal write→search) and return the CLOSED run.

    Writes a marker Document through the real ``RuntimeDocumentView`` path, then —
    after that write has committed in its own request — searches for the marker
    through the real Postgres-FTS ``RuntimeJournalSearchView``. Passes iff the write
    succeeded, the search returned 200 with ``count >= 1``, AND one result's
    ``(kind, slug)`` is the probe's own doc. The doc is deleted afterward.

    A misconfigured target (``resolve_journey_tenant`` raising) or an unreachable
    endpoint closes the run ``error`` and re-raises (DLQ), never a silent pass.
    """
    transport = transport or HttpxRuntimeTransport()

    with record_run(SUITE, trigger, image_tag=None) as run:
        tenant = resolve_journey_tenant()
        internal_key = tenant.internal_api_key or ""

        marker = _new_marker()
        slug = f"eval-journey-{marker}"
        markdown = f"{_MARKDOWN_PREFIX} {marker}"

        try:
            put_started = time.monotonic()
            put_status, _ = transport.put_document(
                tenant.id, kind=_KIND, slug=slug, title=_TITLE, markdown=markdown, internal_key=internal_key
            )
            put_ms = int((time.monotonic() - put_started) * 1000)

            # The write above committed in its own request/transaction (the real
            # container path), so this separate read sees it under MVCC. Searching
            # for a CONTENT token via the FTS endpoint — never a PK/slug lookup —
            # is what proves the Postgres search path itself works.
            search_started = time.monotonic()
            search_status, body = transport.search(tenant.id, q=marker, internal_key=internal_key)
            search_ms = int((time.monotonic() - search_started) * 1000)

            results = body.get("results") or []
            count = int(body.get("count") or 0)
            # INVARIANT #1: read ONLY (kind, slug) off each result — never snippet /
            # title / rank, which carry document content.
            self_match = any(r.get("kind") == _KIND and r.get("slug") == slug for r in results)

            passed = put_status in (200, 201) and search_status == 200 and count >= 1 and self_match

            record(
                run,
                CASE_ID,
                EvalResult.Kind.JOURNEY,
                passed=passed,
                score=count,
                details={
                    "put_status": put_status,
                    "search_status": search_status,
                    "result_count": count,
                    "self_match": self_match,
                    "put_ms": put_ms,
                    "search_ms": search_ms,
                },
            )
        finally:
            # Cleanup only — NOT part of the assertion. Never let a cleanup error
            # mask the probe outcome; a missed delete is harmless because the marker
            # is unique per run.
            try:
                tenant.documents.filter(kind=_KIND, slug=slug).delete()
            except Exception:  # noqa: BLE001
                logger.warning("journey_journal: probe-doc cleanup failed for tenant %s", str(tenant.id)[:8])

    return run
