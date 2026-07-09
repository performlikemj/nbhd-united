"""Document information-keeping: provenance ledger, validated keep, and forget.

Design: ``docs/document-information-keeping-directive.md`` — Phase 2 (D2/D3/D4/§5).

Three capabilities back the agent's ``nbhd_document_keep`` / ``nbhd_document_list_ingestions``
/ ``nbhd_document_forget`` tools and the console Forget button:

- ``record_keep()`` — VALIDATE every artifact against a live, tenant-owned row of a
  registered type, then record the ingestion + surviving artifacts in one transaction
  (D2/D4). Invalid artifacts return in ``errors[]`` (``doc_ingest_bad_ref``) and are
  never recorded, so the ledger can't hold a reference the forget path can't act on.
- ``forget_ingestion()`` — server-side ORM/gateway deletes by stored
  ``object_type`` + ``object_id``, idempotent + re-entrant (D3/§5.4), reporting
  per-item results + honesty caveats so the agent can relay the truth.
- ``list_ingestions()`` — recent ingestions with a ``file_expired`` flag and their
  artifacts, so the agent confirms WHICH document with content shown.

``REMOVAL_HANDLERS`` is the single server-side registry keyed by ``object_type``. Each
handler resolves a tenant-owned row (keep validation) and removes it (forget). The
agent never sets ``removal_strategy`` — the server derives it here. v1 ships the four
core destinations only (§5.3); the rest are one-line additions with zero agent impact.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# The file's true lifetime is ~24-48h (GC runs daily at 05:00). We record the
# floor for the honest-expiry copy ("gone in about a day").
FILE_TTL = timedelta(hours=24)
_MAX_EXCERPT = 2000


# ── Removal handler registry ────────────────────────────────────────────────


class RemovalHandler:
    """One destination's resolve (keep validation) + remove (forget) strategy.

    ``resolve(tenant, object_id)`` returns a live tenant-owned row or ``None``.
    ``remove(tenant, object_id)`` deletes it; object-not-found is success (no raise).
    ``model`` is used for the completeness gap signal (rows created in the window).
    """

    def __init__(self, *, object_type, kind, strategy, model_getter, resolve, remove):
        self.object_type = object_type
        self.kind = kind
        self.strategy = strategy
        self._model_getter = model_getter
        self._resolve = resolve
        self._remove = remove

    @property
    def model(self):
        return self._model_getter()

    def resolve(self, tenant, object_id):
        return self._resolve(tenant, object_id)

    def remove(self, tenant, object_id):
        return self._remove(tenant, object_id)


def _uuid_row(model, tenant, object_id):
    """Resolve a UUID-pk tenant-owned row, tolerating a malformed id (→ None)."""
    try:
        return model.objects.filter(tenant=tenant, id=object_id).first()
    except (ValueError, TypeError, DjangoValidationError):
        return None


def _resolve_journal_document(tenant, object_id):
    from apps.journal.models import Document

    row = _uuid_row(Document, tenant, object_id)
    # Verbatim-keep routes to a dedicated non-daily Document (D5); a daily note
    # is shared same-day content and refuses deletion, so it is not recordable.
    if row is not None and row.kind == Document.Kind.DAILY:
        return None
    return row


def _remove_journal_document(tenant, object_id):
    from apps.journal.models import Document

    try:
        # Never delete a daily note even if one were somehow recorded; its
        # DocumentChunk embeddings cascade on the dedicated-doc delete.
        Document.objects.filter(tenant=tenant, id=object_id).exclude(kind=Document.Kind.DAILY).delete()
    except (ValueError, TypeError, DjangoValidationError):
        return  # malformed id → nothing to remove = success


def _resolve_journal_task(tenant, object_id):
    from apps.journal.models import Task

    return _uuid_row(Task, tenant, object_id)


def _remove_journal_task(tenant, object_id):
    from apps.journal.models import Task

    try:
        Task.objects.filter(tenant=tenant, id=object_id).delete()
    except (ValueError, TypeError, DjangoValidationError):
        return


def _resolve_journal_goal(tenant, object_id):
    from apps.journal.models import Goal

    return _uuid_row(Goal, tenant, object_id)


def _remove_journal_goal(tenant, object_id):
    from apps.journal.models import Goal

    try:
        Goal.objects.filter(tenant=tenant, id=object_id).delete()
    except (ValueError, TypeError, DjangoValidationError):
        return


def _resolve_cron(tenant, object_id):
    """Resolve a reminder CronJob by name (the gateway key), pk as a fallback.

    ``CronJob.name`` is provably identical to the gateway job's ``name`` field, so
    the agent may thread back either the returned ``name`` or the numeric ``id``.
    """
    from apps.cron.models import CronJob

    object_id = str(object_id)
    row = CronJob.objects.filter(tenant=tenant, name=object_id).first()
    if row is None and object_id.isdigit():
        row = CronJob.objects.filter(tenant=tenant, pk=int(object_id)).first()
    return row


def _remove_cron(tenant, object_id):
    """Stop a reminder from firing under BOTH ``postgres_cron_canonical`` states.

    ``postgres_canonical.delete_job`` (a Postgres row delete) is insufficient below
    the flag: its ``post_delete`` signal early-returns and the reconciler no-ops, so
    the authoritative SQLite gateway job stays live and the reminder still fires
    (directive finding-4). So: delete the Postgres desired-state row (fires the
    signal → async reconcile for flag-on tenants) AND remove the job directly from
    the gateway (authoritative + immediate under both flag states). ``cron_remove``
    swallows a gateway "not found".
    """
    from apps.cron.gateway_client import GatewayError, cron_remove
    from apps.cron.models import CronJob

    object_id = str(object_id)
    row = CronJob.objects.filter(tenant=tenant, name=object_id).first()
    if row is None and object_id.isdigit():
        row = CronJob.objects.filter(tenant=tenant, pk=int(object_id)).first()
    name = row.name if row is not None else object_id
    if row is not None:
        row.delete()  # desired-state row gone; reconciler won't re-add the job.
    try:
        cron_remove(tenant, name)
    except GatewayError as exc:
        # A hibernated container isn't running its gateway (fires nothing now); for
        # canonical-flag tenants the desired-state deletion is authoritative and the
        # wake-time reconciler removes the orphaned job. Treat unavailable as done.
        if getattr(exc, "unavailable", False) and getattr(tenant, "postgres_cron_canonical", True):
            logger.warning(
                "doc_forget: gateway unavailable removing reminder for tenant %s; "
                "desired-state row deleted, wake reconcile will converge",
                str(getattr(tenant, "id", ""))[:8],
            )
            return
        raise  # real transport failure → artifact marked failed, retried next run


def _cron_model():
    from apps.cron.models import CronJob

    return CronJob


def _document_model():
    from apps.journal.models import Document

    return Document


def _task_model():
    from apps.journal.models import Task

    return Task


def _goal_model():
    from apps.journal.models import Goal

    return Goal


# Server-side registry (D3/§5.3). v1 ships the four core destinations only; the
# rest are deferred one-line additions. Any object_type absent here is rejected at
# keep (D4), so the "forget" promise can never diverge from a delete capability.
REMOVAL_HANDLERS: dict[str, RemovalHandler] = {
    "journal.Document": RemovalHandler(
        object_type="journal.Document",
        kind="journal_note",
        strategy="row_delete",
        model_getter=_document_model,
        resolve=_resolve_journal_document,
        remove=_remove_journal_document,
    ),
    "journal.Task": RemovalHandler(
        object_type="journal.Task",
        kind="task",
        strategy="row_delete",
        model_getter=_task_model,
        resolve=_resolve_journal_task,
        remove=_remove_journal_task,
    ),
    "journal.Goal": RemovalHandler(
        object_type="journal.Goal",
        kind="goal",
        strategy="row_delete",
        model_getter=_goal_model,
        resolve=_resolve_journal_goal,
        remove=_remove_journal_goal,
    ),
    "cron.CronJob": RemovalHandler(
        object_type="cron.CronJob",
        kind="reminder",
        strategy="cron_delete",
        model_getter=_cron_model,
        resolve=_resolve_cron,
        remove=_remove_cron,
    ),
}


# ── Marker / completeness helpers ───────────────────────────────────────────


def _marker_message(tenant, client_msg_id):
    """The document-upload turn for this ``client_msg_id``, or None.

    Keys off ``attachment_path`` (a stored ``doc_<hash>`` file), NOT the
    ``[Document attached:]`` marker — the marker lives only in the queued payload,
    never on the persisted ``AppChatMessage`` row.
    """
    if not client_msg_id:
        return None
    from apps.router.inbound_media import is_inbound_document_path
    from apps.router.models import AppChatMessage

    msg = AppChatMessage.objects.filter(tenant=tenant, client_msg_id=client_msg_id).first()
    if msg is None or not is_inbound_document_path(msg.attachment_path):
        return None
    return msg


def _completeness(tenant, marker_msg, recorded_count):
    """Return (user_turns_since_marker, window_count).

    ``window_count`` over-counts safely (unrelated writes in the window inflate it),
    so a gap is a monitored signal that triggers a look, never a bad delete (D2).
    """
    from apps.router.models import AppChatMessage

    since = marker_msg.created_at
    user_turns = AppChatMessage.objects.filter(tenant=tenant, created_at__gt=since).count()
    seen_models = set()
    window_count = 0
    for handler in REMOVAL_HANDLERS.values():
        model = handler.model
        if model in seen_models:
            continue
        seen_models.add(model)
        window_count += model.objects.filter(tenant=tenant, created_at__gte=since).count()
    return user_turns, window_count


# ── Keep (validated manifest) ───────────────────────────────────────────────


def record_keep(tenant, *, source: dict, artifacts: list[dict]) -> dict:
    """Validate + record a document ingestion manifest (D2/D4).

    Returns ``{"ingestion_id": <uuid|None>, "recorded": N, "errors": [...]}``. An
    unregistered ``object_type`` or an ``object_id`` that does not resolve to a
    tenant-owned row of that type is returned in ``errors[]`` (never recorded) —
    per artifact, not all-or-nothing, so one bad reference can't strand the valid
    saves as unforgettable orphans.
    """
    source = source or {}
    artifacts = artifacts or []
    client_msg_id = str(source.get("client_msg_id") or "").strip()[:64]

    validated: list[tuple[dict, RemovalHandler]] = []
    errors: list[dict] = []
    for art in artifacts:
        art = art if isinstance(art, dict) else {}
        object_type = str(art.get("object_type") or "").strip()
        object_id = str(art.get("object_id") or "").strip()
        handler = REMOVAL_HANDLERS.get(object_type)
        if handler is None:
            errors.append({"object_type": object_type, "object_id": object_id, "reason": "unregistered_type"})
            _emit_bad_ref(tenant, object_type, "unregistered_type")
            continue
        if handler.resolve(tenant, object_id) is None:
            errors.append({"object_type": object_type, "object_id": object_id, "reason": "not_found"})
            _emit_bad_ref(tenant, object_type, "not_found")
            continue
        validated.append((art, handler))

    marker_msg = _marker_message(tenant, client_msg_id)
    uploaded_at = marker_msg.created_at if marker_msg is not None else timezone.now()
    thread = getattr(marker_msg, "thread", None)

    ingestion_id = None
    if validated:
        from apps.journal.models import DocumentIngestion, DocumentIngestionArtifact

        with transaction.atomic():
            ingestion = DocumentIngestion.objects.create(
                tenant=tenant,
                thread=thread,
                client_msg_id=client_msg_id,
                original_filename=str(source.get("original_filename") or "")[:255],
                content_hash=str(source.get("content_hash") or "")[:64],
                workspace_path=str(source.get("workspace_path") or "")[:255],
                uploaded_at=uploaded_at,
                file_expires_at=uploaded_at + FILE_TTL,
                status=DocumentIngestion.Status.KEPT,
                agreed_at=timezone.now(),
            )
            DocumentIngestionArtifact.objects.bulk_create(
                [
                    DocumentIngestionArtifact(
                        ingestion=ingestion,
                        tenant=tenant,
                        kind=str(art.get("kind") or handler.kind)[:32],
                        object_type=handler.object_type,
                        object_id=str(art.get("object_id") or "").strip()[:128],
                        destination=str(art.get("destination") or "")[:255],
                        content_excerpt=str(art.get("excerpt") or "")[:_MAX_EXCERPT],
                        removal_strategy=handler.strategy,
                    )
                    for art, handler in validated
                ]
            )
        ingestion_id = str(ingestion.id)

    recorded = len(validated)
    user_turns = None
    if marker_msg is not None:
        user_turns, window_count = _completeness(tenant, marker_msg, recorded)
        if window_count > recorded:
            _emit_gap(tenant, window_count, recorded)
    _emit_save(tenant, len(artifacts), recorded, len(errors), user_turns)

    return {"ingestion_id": ingestion_id, "recorded": recorded, "errors": errors}


# ── Forget (server-side fan-out) ────────────────────────────────────────────


def forget_ingestion(tenant, ingestion_id) -> dict:
    """Delete every item recorded from one ingestion — and nothing else (D3/§5.4).

    Idempotent + re-entrant: iterates artifacts where ``removed_at IS NULL``,
    dispatches on ``REMOVAL_HANDLERS[object_type]``, stamps ``removed_at`` on success
    or ``last_error`` on failure. Object-not-found is success (ids were validated at
    keep, so a not-found genuinely means "deleted since keep"). A re-run skips
    already-removed rows, so retry targets only survivors. Returns per-artifact
    results + honesty caveats the agent relays.
    """
    from apps.journal.models import DocumentIngestion

    ingestion = (
        DocumentIngestion.objects.filter(tenant=tenant, id=_coerce_uuid(ingestion_id))
        .prefetch_related("artifacts")
        .first()
    )
    if ingestion is None:
        return {"error": "not_found"}

    results = []
    removed = 0
    failed = 0
    had_reminder = False
    for art in ingestion.artifacts.all():
        if art.object_type == "cron.CronJob":
            had_reminder = True
        if art.removed_at is not None:
            results.append(_artifact_result(art, removed=True))
            continue
        handler = REMOVAL_HANDLERS.get(art.object_type)
        if handler is None:
            # Recorded only when a handler existed (D4), so this is an
            # after-the-fact registry removal — surface it, never silently drop.
            art.last_error = "no_handler"
            art.save(update_fields=["last_error"])
            failed += 1
            results.append(_artifact_result(art, removed=False, error="no_handler"))
            continue
        try:
            handler.remove(tenant, art.object_id)
        except Exception as exc:  # noqa: BLE001 — report, never abort the batch
            art.last_error = str(exc)[:255]
            art.save(update_fields=["last_error"])
            failed += 1
            results.append(_artifact_result(art, removed=False, error=art.last_error))
            logger.warning(
                "doc_forget: failed to remove %s for tenant %s: %s",
                art.object_type,
                str(getattr(tenant, "id", ""))[:8],
                exc,
            )
            continue
        art.removed_at = timezone.now()
        art.last_error = ""
        art.save(update_fields=["removed_at", "last_error"])
        removed += 1
        results.append(_artifact_result(art, removed=True))

    all_removed = not ingestion.artifacts.filter(removed_at__isnull=True).exists()
    ingestion.status = DocumentIngestion.Status.REMOVED if all_removed else DocumentIngestion.Status.PARTIALLY_REMOVED
    ingestion.save(update_fields=["status", "updated_at"])

    _emit_forget(tenant, ingestion.id, removed, failed)

    return {
        "ingestion_id": str(ingestion.id),
        "status": ingestion.status,
        "removed": removed,
        "failed": failed,
        "results": results,
        "caveats": _forget_caveats(had_reminder, tenant),
    }


def list_ingestions(tenant, *, limit: int = 20) -> dict:
    """Recent ingestions with a ``file_expired`` flag + their artifacts (§ Tool 2)."""
    from apps.journal.models import DocumentIngestion

    now = timezone.now()
    rows = (
        DocumentIngestion.objects.filter(tenant=tenant)
        .prefetch_related("artifacts")
        .order_by("-created_at")[: max(1, min(int(limit or 20), 100))]
    )
    ingestions = []
    for row in rows:
        ingestions.append(
            {
                "id": str(row.id),
                "original_filename": row.original_filename,
                "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else None,
                "file_expired": bool(row.file_expires_at and now > row.file_expires_at),
                "status": row.status,
                "artifacts": [
                    {
                        "kind": a.kind,
                        "destination": a.destination,
                        "excerpt": a.content_excerpt,
                        "removed": a.removed_at is not None,
                    }
                    for a in row.artifacts.all()
                ],
            }
        )
    return {"ingestions": ingestions, "count": len(ingestions)}


# ── Small helpers ───────────────────────────────────────────────────────────


def _coerce_uuid(value):
    """Return a value safe to filter a UUID pk on; malformed → an impossible id."""
    import uuid as _uuid

    try:
        return _uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return _uuid.UUID(int=0)


def _artifact_result(art, *, removed: bool, error: str = "") -> dict:
    return {
        "kind": art.kind,
        "destination": art.destination,
        "excerpt": art.content_excerpt,
        "object_type": art.object_type,
        "removed": removed,
        "error": error,
    }


def _forget_caveats(had_reminder: bool, tenant) -> list[str]:
    """The honesty boundaries the forget reply must state (§5.5)."""
    caveats = [
        "The document's contents already reached the AI model when we first talked "
        "(reads aren't redacted) — forget removes the saved information here, not the "
        "model's earlier reading, and can't un-read it.",
        "To also make me forget a person's name, use People settings — this removes the saved information only.",
    ]
    if had_reminder:
        caveats.append(
            "Future reminder fires are cancelled, but a reminder that already went out "
            "stays in your history — I can't unsend it."
        )
    return caveats


# ── Telemetry (§4 — structured, no cleartext filenames/content) ─────────────


def _tid(tenant) -> str:
    return str(getattr(tenant, "id", ""))[:8]


def _emit_save(tenant, artifacts, recorded, errors, user_turns_since_marker):
    logger.info(
        "doc_ingest_save tenant=%s artifacts=%d recorded=%d errors=%d user_turns_since_marker=%s",
        _tid(tenant),
        artifacts,
        recorded,
        errors,
        "unknown" if user_turns_since_marker is None else user_turns_since_marker,
    )


def _emit_bad_ref(tenant, object_type, reason):
    logger.info("doc_ingest_bad_ref tenant=%s object_type=%s reason=%s", _tid(tenant), object_type, reason)


def _emit_gap(tenant, created_in_window, recorded):
    logger.info(
        "doc_ingest_gap tenant=%s created_in_window=%d recorded=%d",
        _tid(tenant),
        created_in_window,
        recorded,
    )


def _emit_forget(tenant, ingestion_id, removed, failed):
    logger.info(
        "doc_ingest_forget tenant=%s ingestion=%s removed=%d failed=%d",
        _tid(tenant),
        str(ingestion_id)[:8],
        removed,
        failed,
    )
