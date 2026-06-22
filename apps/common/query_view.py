"""Base class for parameterized query views used by per-domain query tools.

Each domain (Finance, Fuel, Journal, Insights) exposes a single
``POST /api/v1/<domain>/runtime/<tenant_id>/query/`` endpoint backed by a
``BaseQueryView`` subclass. The subclass declares its own Pydantic request
model and implements ``execute()``; the base class handles auth, RLS,
window resolution, deterministic hashing, JSON-safe serialization, and the
``meta`` response envelope.

See ``CONTINUITY_agent-context-via-queries.md`` §4–6 for the contract.

Determinism contract: for any
``(tenant, resource, window_resolved, filter, fields, aggregate,
aggregate_field, group_by, order_by, limit)`` tuple and any DB snapshot,
the response body is byte-identical. ``meta.query_hash`` is the sha256 of
the canonical JSON of that tuple — used for replay, caching, and tests.

JSON safety: ``Decimal`` and ``UUID`` always render as strings, ``date`` /
``datetime`` as ISO. The agent must never receive a float for an amount.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

from django.core.exceptions import FieldError
from pydantic import BaseModel, ValidationError
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.tenant_tz import safe_zoneinfo, tenant_tz_name
from apps.common.windows import Window, resolve_window
from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

logger = logging.getLogger("nbhd.query")


class QueryExecutionError(Exception):
    """Raised by ``execute()`` to return an error response with a specific status."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def jsonify(value: Any) -> Any:
    """Recursively coerce ``value`` into JSON-safe types.

    Decimal → str (precision-preserving), UUID → str, date/datetime → ISO,
    list/dict recursed. Everything else passes through unchanged.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonify(v) for v in value]
    return value


def canonical_query_hash(
    *,
    resource: str,
    window_resolved: tuple[date, date] | None,
    filter_: dict[str, Any] | None,
    fields: list[str] | None,
    aggregate: str | None,
    aggregate_field: str | None,
    group_by: str | None,
    order_by: str | None,
    limit: int | None,
) -> str:
    """Deterministic sha256 over the canonical query shape.

    Window is included as resolved dates (not the kind/value form) — same
    logical window at different real times produces the same hash if it
    resolves to the same date range.
    """
    payload = {
        "resource": resource,
        "window_resolved": (
            [window_resolved[0].isoformat(), window_resolved[1].isoformat()] if window_resolved else None
        ),
        "filter": filter_ if filter_ is not None else {},
        "fields": sorted(fields) if fields else None,
        "aggregate": aggregate,
        "aggregate_field": aggregate_field,
        "group_by": group_by,
        "order_by": order_by,
        "limit": limit,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BaseQueryView(APIView):
    """Hoist the common dispatch path for per-domain ``query`` endpoints.

    Subclasses set ``query_model`` to their Pydantic request model and
    implement ``execute(self, query, tenant, window_resolved) -> (data, row_count)``.
    ``data`` is JSON-coercible (use ``jsonify`` for Decimal/UUID/date).
    """

    permission_classes = [AllowAny]
    query_model: ClassVar[type[BaseModel]]

    # ── Public entrypoint ──────────────────────────────────────────────

    def post(self, request, tenant_id):
        auth_error = self._auth(request, tenant_id)
        if auth_error is not None:
            return auth_error

        tenant = self._tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        tz_name = tenant_tz_name(tenant)

        try:
            query = self.query_model(**(request.data or {}))
        except ValidationError as exc:
            return Response(
                {"error": "validation_failed", "detail": exc.errors(include_url=False)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        window_obj: Window | None = getattr(query, "window", None)
        window_resolved = resolve_window(window_obj, tz_name) if window_obj is not None else None

        try:
            now = datetime.now(UTC)
            data, row_count = self.execute(query, tenant, window_resolved)
        except QueryExecutionError as exc:
            return Response(
                {"error": exc.code, "detail": exc.message},
                status=exc.status_code,
            )
        except FieldError as exc:
            # Belt-and-suspenders: a serializer-only field (e.g. a relation
            # traversal not backed by a real column) reaching qs.order_by /
            # qs.values would raise FieldError -> HTTP 500. Translate it into
            # a clean 400 so the strict query contract holds even if a future
            # allowlist entry slips through.
            logger.warning("query FieldError on %s: %s", self.__class__.__name__, exc)
            return Response(
                {"error": "invalid_query", "detail": "query references a field that is not a real database column"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        limit = getattr(query, "limit", None)
        has_more = bool(limit) and isinstance(data, list) and row_count >= limit

        body = {
            "data": jsonify(data),
            "meta": {
                "schema_version": getattr(query, "schema_version", 1),
                "computed_at": now.isoformat(),
                "tenant_tz": tz_name,
                "as_of": now.astimezone(safe_zoneinfo(tz_name)).isoformat(),
                "window_resolved_to": (
                    {"from": window_resolved[0].isoformat(), "to": window_resolved[1].isoformat()}
                    if window_resolved
                    else None
                ),
                "row_count": row_count,
                "has_more": has_more,
                "query_hash": canonical_query_hash(
                    resource=getattr(query, "resource", ""),
                    window_resolved=window_resolved,
                    filter_=jsonify(getattr(query, "filter", {})),
                    fields=getattr(query, "fields", None),
                    aggregate=getattr(query, "aggregate", None),
                    aggregate_field=getattr(query, "aggregate_field", None),
                    group_by=getattr(query, "group_by", None),
                    order_by=getattr(query, "order_by", None),
                    limit=limit,
                ),
            },
        }

        logger.info(
            "PERF.query tenant=%s domain=%s resource=%s row_count=%d hash=%s",
            str(tenant.id)[:8],
            self.__class__.__name__,
            getattr(query, "resource", "?"),
            row_count,
            body["meta"]["query_hash"][:16],
        )

        return Response(body)

    # ── Subclass contract ──────────────────────────────────────────────

    def execute(
        self,
        query: BaseModel,
        tenant: Tenant,
        window_resolved: tuple[date, date] | None,
    ) -> tuple[Any, int]:
        """Run the actual DB query. Return ``(data, row_count)``.

        ``data`` may be a list of row dicts (default), or a list of aggregate
        rows like ``[{"group": {...}, "sum": "100.00", "count": 3}]``.
        ``row_count`` is the post-limit count for list responses or the number
        of groups for aggregates.

        Raise ``QueryExecutionError`` for user-recoverable errors.
        """
        raise NotImplementedError

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    def _auth(request, tenant_id) -> Response | None:
        try:
            validate_internal_runtime_request(
                provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
                provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
                expected_tenant_id=str(tenant_id),
            )
        except InternalAuthError as exc:
            return Response(
                {"error": "internal_auth_failed", "detail": str(exc)},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        set_rls_context(tenant_id=tenant_id, service_role=True)
        return None

    @staticmethod
    def _tenant_or_404(tenant_id) -> Tenant | Response:
        try:
            return Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            return Response(
                {"error": "tenant_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )
