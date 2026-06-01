"""Mirror journal documents to the tenant workspace file share.

Renders Document content as markdown files and uploads them to the tenant's
Azure File Share. The files are a journal-of-record / human-readable mirror
of what's in Postgres — they used to back OpenClaw's built-in memory_search
SQLite index, but that index is disabled fleet-wide (SQLite over SMB is
hostile; the file would corrupt mid-write). Search now routes through
nbhd_journal_search → Django → Postgres directly.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)


def render_memory_files(tenant) -> dict[str, str]:
    """Render a tenant's documents as workspace-relative markdown files.

    Returns a dict of ``{relative_path: content}`` suitable for writing
    to the tenant's workspace.  Includes:

    - All non-daily documents (memory, goals, projects, etc.)
    - Daily documents from the last 30 days

    Paths follow the pattern ``memory/journal/<kind>/<slug>.md``.
    """
    from apps.journal.models import Document

    cutoff = (timezone.now() - timedelta(days=30)).date()

    # All non-daily docs + recent dailies. For tenants on the typed Goal/Task
    # lifecycle, Document(kind in [goal, tasks]) is owned by the typed tables
    # instead — skip them here so we don't write stale markdown mirrors of
    # rows that have been migrated/superseded.
    documents = Document.objects.filter(tenant=tenant)
    if getattr(tenant, "experimental_typed_journal_lifecycle", False):
        documents = documents.exclude(kind__in=[Document.Kind.GOAL, Document.Kind.TASKS])
    documents = documents.exclude(
        kind="daily",
        slug__lt=str(cutoff),
    ).order_by("kind", "slug")

    from apps.journal.path_validation import validate_kind_slug
    from apps.pii.redactor import RedactionSession

    session = RedactionSession(tenant=tenant)

    files: dict[str, str] = {}
    for doc in documents:
        # Defense-in-depth: skip rows whose kind/slug would produce an
        # NTFS-hostile SMB path (``:``, ``..``, empty, out-of-enum kind, etc.).
        # The runtime endpoint enforces this at the trust boundary, but a
        # direct DB write or a pre-validation row from before the guard
        # landed could still grind the sync against bad path components.
        if validate_kind_slug(doc.kind, doc.slug or "") is not None:
            logger.warning(
                "memory_sync: skipping doc with invalid path components tenant=%s id=%s kind=%r slug=%r",
                tenant.id,
                doc.id,
                doc.kind,
                doc.slug,
            )
            continue
        path = f"memory/journal/{doc.kind}/{doc.slug}.md"
        content = f"# {doc.title}\n\n{doc.markdown}"
        content = session.redact(content)
        files[path] = content

    # Merge workspace entity mapping with any message-originated entities
    if session.entity_map:
        existing_map = (
            type(tenant).objects.filter(pk=tenant.pk).values_list("pii_entity_map", flat=True).first()
        ) or {}
        # Workspace entities override, message entities preserved
        merged = {**existing_map, **session.entity_map}
        type(tenant).objects.filter(pk=tenant.pk).update(
            pii_entity_map=merged,
        )

    return files


def upload_memory_files_to_share(tenant_id: str, files: dict[str, str]) -> int:
    """Upload rendered memory files to the tenant's Azure File Share.

    Creates directories as needed and only overwrites files whose content
    has changed.  Returns the number of files written.

    In mock mode (``AZURE_MOCK=true``), logs but does not write.
    """

    from django.conf import settings

    from apps.orchestrator.azure_client import _is_mock

    share_name = f"ws-{str(tenant_id)[:20]}"

    if _is_mock():
        logger.info(
            "[MOCK] Would upload %d memory files to share %s",
            len(files),
            share_name,
        )
        return len(files)

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

    from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
    from azure.storage.fileshare import ShareClient, ShareDirectoryClient, ShareFileClient

    from apps.orchestrator.azure_client import get_storage_client, sanitize_share_text

    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account_name,
    )
    account_key = keys.keys[0].value
    account_url = f"https://{account_name}.file.core.windows.net"

    # Verify the file share exists before attempting any writes.
    # Tenants provisioned before the memory-sync feature was added won't have
    # a share yet — soft-fail so QStash doesn't retry endlessly.
    share_client = ShareClient(
        account_url=account_url,
        share_name=share_name,
        credential=account_key,
    )
    try:
        share_client.get_share_properties()
    except ResourceNotFoundError:
        logger.warning(
            "upload_memory_files_to_share: share %s does not exist for tenant %s — skipping",
            share_name,
            tenant_id,
        )
        return 0

    written = 0
    created_dirs: set[str] = set()

    for rel_path, content in files.items():
        # Ensure parent directories exist
        parts = rel_path.split("/")
        for depth in range(1, len(parts)):
            dir_path = "/".join(parts[:depth])
            if dir_path not in created_dirs:
                try:
                    dir_client = ShareDirectoryClient(
                        account_url=account_url,
                        share_name=share_name,
                        directory_path=dir_path,
                        credential=account_key,
                    )
                    dir_client.create_directory()
                except ResourceExistsError:
                    pass  # Directory already exists
                except ResourceNotFoundError:
                    logger.warning(
                        "memory_sync: share or parent dir not found creating %s/%s",
                        share_name,
                        dir_path,
                        exc_info=True,
                    )
                except Exception:
                    logger.warning(
                        "memory_sync: failed to create directory %s/%s",
                        share_name,
                        dir_path,
                        exc_info=True,
                    )
                created_dirs.add(dir_path)

        file_client = ShareFileClient(
            account_url=account_url,
            share_name=share_name,
            file_path=rel_path,
            credential=account_key,
        )

        # Same corruption guard as the azure_client write chokepoint — this
        # batch loop reuses one client for perf, so it sanitizes inline rather
        # than routing each file through _put_share_file.
        encoded = sanitize_share_text(content).encode("utf-8")

        # Check if content changed before writing
        try:
            props = file_client.get_file_properties()
            if props.size == len(encoded):
                existing = file_client.download_file().readall()
                if existing == encoded:
                    continue
        except ResourceNotFoundError:
            pass  # File doesn't exist yet
        except Exception:
            logger.warning(
                "memory_sync: failed to check file %s/%s",
                share_name,
                rel_path,
                exc_info=True,
            )

        try:
            file_client.upload_file(encoded, length=len(encoded))
            written += 1
        except ResourceNotFoundError:
            logger.warning(
                "memory_sync: parent directory missing for %s/%s — skipping file",
                share_name,
                rel_path,
            )

    logger.info(
        "upload_memory_files_to_share: tenant=%s written=%d total=%d",
        tenant_id,
        written,
        len(files),
    )
    return written
