"""Orchestrator services — provision/deprovision OpenClaw instances."""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.tenants.models import Tenant
from .azure_client import (
    _is_mock,
    assign_acr_pull_role,
    assign_key_vault_role,
    create_container_app,
    create_managed_identity,
    create_tenant_file_share,
    delete_container_app,
    delete_managed_identity,
    delete_tenant_file_share,
    register_environment_storage,
    upload_config_to_file_share,
)
from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
from .config_generator import build_cron_seed_jobs, config_to_json, generate_openclaw_config
from .personas import render_workspace_files

logger = logging.getLogger(__name__)


def _log_provisioning_event(*, tenant_id: str, user_id: str | None, stage: str, error: str = "") -> None:
    logger.info(
        "tenant_provisioning tenant_id=%s user_id=%s stage=%s error=%s",
        tenant_id,
        user_id or "",
        stage,
        error,
    )


def _stale_provisioning_tenants_queryset(*, tenant_id: str | None = None):
    query = Tenant.objects.filter(
        status__in=[Tenant.Status.PENDING, Tenant.Status.PROVISIONING, Tenant.Status.ACTIVE],
    ).filter(
        models.Q(container_id="") | models.Q(container_fqdn=""),
    )
    if tenant_id:
        query = query.filter(id=tenant_id)
    return query.select_related("user").order_by("created_at")


def repair_stale_tenant_provisioning(
    *,
    tenant_id: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    query = _stale_provisioning_tenants_queryset(tenant_id=tenant_id)
    if limit:
        query = query[:limit]

    tenants = list(query)
    summary = {
        "evaluated": len(tenants),
        "repaired": 0,
        "failed": 0,
        "skipped": 0,
        "dry_run": dry_run,
        "results": [],
    }

    for tenant in tenants:
        tenant_id_str = str(tenant.id)
        user_id_str = str(tenant.user_id)
        missing = []
        if not tenant.container_id:
            missing.append("container_id")
        if not tenant.container_fqdn:
            missing.append("container_fqdn")

        if dry_run:
            _log_provisioning_event(
                tenant_id=tenant_id_str,
                user_id=user_id_str,
                stage="repair_dry_run",
                error=",".join(missing),
            )
            summary["skipped"] += 1
            summary["results"].append({
                "tenant_id": tenant_id_str,
                "user_id": user_id_str,
                "status": tenant.status,
                "result": "dry_run",
                "missing": missing,
            })
            continue

        try:
            _log_provisioning_event(
                tenant_id=tenant_id_str,
                user_id=user_id_str,
                stage="repair_start",
            )
            provision_tenant(tenant_id_str)
            tenant.refresh_from_db()

            ready = bool(tenant.container_id and tenant.container_fqdn and tenant.status == Tenant.Status.ACTIVE)
            if ready:
                summary["repaired"] += 1
                outcome = "repaired"
            else:
                summary["failed"] += 1
                outcome = "incomplete"

            summary["results"].append({
                "tenant_id": tenant_id_str,
                "user_id": user_id_str,
                "status": tenant.status,
                "result": outcome,
                "missing": [
                    field
                    for field, value in (("container_id", tenant.container_id), ("container_fqdn", tenant.container_fqdn))
                    if not value
                ],
            })
        except Exception as exc:
            summary["failed"] += 1
            _log_provisioning_event(
                tenant_id=tenant_id_str,
                user_id=user_id_str,
                stage="repair_failed",
                error=str(exc),
            )
            summary["results"].append({
                "tenant_id": tenant_id_str,
                "user_id": user_id_str,
                "status": tenant.status,
                "result": "failed",
                "error": str(exc),
                "missing": missing,
            })

    return summary


def provision_tenant(tenant_id: str) -> None:
    """Full provisioning flow for a new tenant."""
    tenant = Tenant.objects.select_related("user").get(id=tenant_id)
    user_id = str(tenant.user_id)
    _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="provision_start")
    secret_backend = str(
        getattr(settings, "OPENCLAW_CONTAINER_SECRET_BACKEND", "keyvault") or "keyvault"
    ).strip().lower()

    if tenant.status not in (Tenant.Status.PENDING, Tenant.Status.PROVISIONING):
        _log_provisioning_event(
            tenant_id=str(tenant.id),
            user_id=user_id,
            stage="provision_skipped_unexpected_status",
            error=tenant.status,
        )
        logger.warning("Tenant %s in unexpected state %s for provisioning", tenant_id, tenant.status)
        return

    tenant.status = Tenant.Status.PROVISIONING
    tenant.save(update_fields=["status", "updated_at"])

    try:
        # 1. Generate OpenClaw config
        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="generate_config")
        config = generate_openclaw_config(tenant)
        config_json = config_to_json(config)

        # 2. Create Managed Identity
        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="create_managed_identity")
        identity = create_managed_identity(str(tenant.id))

        # 2b. Grant identity Key Vault access for secret references (keyvault backend only)
        if secret_backend == "keyvault":
            _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="assign_key_vault_role")
            assign_key_vault_role(identity["principal_id"])

        # 2b2. Grant identity ACR pull access for container image
        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="assign_acr_pull_role")
        assign_acr_pull_role(identity["principal_id"])

        # 2c. Create Azure File Share and register with Container Environment
        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="create_file_share")
        create_tenant_file_share(str(tenant.id))
        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="register_environment_storage")
        register_environment_storage(str(tenant.id))

        # 2f2. Write config to file share so OpenClaw reads it on first boot
        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="upload_config")
        upload_config_to_file_share(str(tenant.id), config_json)

        # 2g. Render workspace templates based on persona
        persona_key = (tenant.user.preferences or {}).get("agent_persona", "neighbor")
        workspace_env = render_workspace_files(persona_key, tenant=tenant)

        # 3. Create Container App
        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="create_container_app")
        container_name = f"oc-{str(tenant.id)[:20]}"
        result = create_container_app(
            tenant_id=str(tenant.id),
            container_name=container_name,
            config_json=config_json,
            identity_id=identity["id"],
            identity_client_id=identity["client_id"],
            workspace_env=workspace_env,
        )

        # 4. Update tenant record
        tenant.container_id = result["name"]
        tenant.container_fqdn = result["fqdn"]
        tenant.managed_identity_id = identity["id"]
        tenant.container_image_tag = getattr(settings, "OPENCLAW_IMAGE_TAG", "latest") or "latest"
        tenant.status = Tenant.Status.ACTIVE
        tenant.provisioned_at = timezone.now()
        tenant.save(update_fields=[
            "container_id", "container_fqdn", "managed_identity_id",
            "container_image_tag", "status", "provisioned_at", "updated_at",
        ])

        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="provision_success")
        logger.info("Provisioned tenant %s → container %s", tenant_id, result["name"])

    except Exception as exc:
        _log_provisioning_event(
            tenant_id=str(tenant.id),
            user_id=user_id,
            stage="provision_failed",
            error=str(exc),
        )
        logger.exception("Failed to provision tenant %s", tenant_id)
        tenant.status = Tenant.Status.PENDING
        tenant.save(update_fields=["status", "updated_at"])
        raise

    # --- Post-provision steps (non-critical) ---
    # These run OUTSIDE the main try/except so failures here do NOT reset
    # the tenant to PENDING. The container metadata is already persisted.

    # 4b. Send proactive welcome message via Telegram
    chat_id = tenant.user.telegram_chat_id
    if chat_id:
        try:
            from apps.router.onboarding import WELCOME_MESSAGE
            from apps.router.services import send_telegram_message

            send_telegram_message(chat_id, WELCOME_MESSAGE)
            tenant.onboarding_step = 1  # Advance past step 0 (welcome sent)
            tenant.save(update_fields=["onboarding_step", "updated_at"])
            logger.info("Sent welcome message to chat_id=%s for tenant %s", chat_id, tenant_id)
        except Exception:
            logger.warning("Could not send welcome message for tenant %s", tenant_id, exc_info=True)

    # 5. Seed default cron jobs to Gateway (delayed for container warm-up)
    try:
        from apps.cron.views import _schedule_qstash_task

        _schedule_qstash_task("seed_cron_jobs", str(tenant.id), delay_seconds=60)
    except Exception:
        # TODO: schedule with delay
        logger.warning(
            "Could not schedule cron job seeding for tenant %s",
            tenant_id,
            exc_info=True,
        )
        try:
            seed_cron_jobs(tenant)
        except Exception:
            logger.warning(
                "Could not seed cron jobs directly for tenant %s",
                tenant_id,
                exc_info=True,
            )


def update_tenant_config(tenant_id: str) -> None:
    """Regenerate OpenClaw config and update the running container."""
    tenant = Tenant.objects.select_related("user").get(id=tenant_id)

    if tenant.status != Tenant.Status.ACTIVE or not tenant.container_id:
        logger.warning(
            "Cannot update config for tenant %s (status=%s, container=%s)",
            tenant_id, tenant.status, tenant.container_id,
        )
        return

    config = generate_openclaw_config(tenant)
    config_json = config_to_json(config)

    # Write to file share (source of truth — OpenClaw reads from file after first boot)
    upload_config_to_file_share(str(tenant.id), config_json)

    # Write workspace files (AGENTS.md, SOUL.md, etc.) to file share
    # so updates propagate without needing container env var changes.
    try:
        from .azure_client import upload_workspace_file
        from .personas import render_workspace_files, render_workspace_rules

        persona_key = (tenant.user.preferences or {}).get("agent_persona", "neighbor")
        workspace_files = render_workspace_files(persona_key, tenant=tenant)

        file_map = {
            "NBHD_AGENTS_MD": "workspace/AGENTS.md",
            "NBHD_SOUL_MD": "workspace/SOUL.md",
            "NBHD_IDENTITY_MD": "workspace/IDENTITY.md",
            # Reference docs — written to workspace/docs/ and read on-demand
            "NBHD_DOC_TOOLS_REFERENCE": "workspace/docs/tools-reference.md",
            "NBHD_DOC_CHANNEL_FORMATTING": "workspace/docs/channel-formatting.md",
            "NBHD_DOC_CRON_MANAGEMENT": "workspace/docs/cron-management.md",
            "NBHD_DOC_ERROR_HANDLING": "workspace/docs/error-handling.md",
            "NBHD_DOC_PRIVACY_REDACTION": "workspace/docs/privacy-redaction.md",
        }

        # Deploy full or silent platform guide based on feature_tips_enabled
        guide_key = (
            "NBHD_DOC_PLATFORM_GUIDE"
            if tenant.feature_tips_enabled
            else "NBHD_DOC_PLATFORM_GUIDE_SILENT"
        )
        file_map[guide_key] = "workspace/docs/platform-guide.md"

        for env_key, file_path in file_map.items():
            content = workspace_files.get(env_key, "")
            if content:
                upload_workspace_file(str(tenant.id), file_path, content)

        # Upload all rule templates to workspace/rules/ — referenced by AGENTS.md
        # for on-demand loading. Auto-discovers all .md files in templates/openclaw/rules/.
        rules = render_workspace_rules()
        for filename, content in rules.items():
            upload_workspace_file(
                str(tenant.id),
                f"workspace/rules/{filename}",
                content,
            )
    except Exception:
        logger.exception("Failed to upload workspace files for tenant %s", tenant_id)

    # Update system cron job prompts to match current config_generator
    try:
        result = update_system_cron_prompts(tenant)
        if result["updated"]:
            logger.info("Updated %d cron prompts for tenant %s", result["updated"], tenant_id)
    except Exception:
        logger.exception("Failed to update cron prompts for tenant %s (non-fatal)", tenant_id)

    logger.info("Updated OpenClaw config for tenant %s", tenant_id)


def deprovision_tenant(tenant_id: str) -> None:
    """Full deprovisioning flow."""
    tenant = Tenant.objects.get(id=tenant_id)

    tenant.status = Tenant.Status.DEPROVISIONING
    tenant.save(update_fields=["status", "updated_at"])

    try:
        # 1. Delete container
        if tenant.container_id:
            delete_container_app(tenant.container_id)

        # 1b. Delete file share and environment storage
        delete_tenant_file_share(str(tenant.id))

        # 2. Delete managed identity
        delete_managed_identity(str(tenant.id))

        # 3. Update tenant
        tenant.status = Tenant.Status.DELETED
        tenant.container_id = ""
        tenant.container_fqdn = ""
        tenant.managed_identity_id = ""
        tenant.save(update_fields=[
            "status", "container_id", "container_fqdn",
            "managed_identity_id", "updated_at",
        ])

        logger.info("Deprovisioned tenant %s", tenant_id)

    except Exception:
        logger.exception("Failed to deprovision tenant %s", tenant_id)
        tenant.status = Tenant.Status.SUSPENDED
        tenant.save(update_fields=["status", "updated_at"])
        raise


def dedup_tenant_cron_jobs(
    tenant: Tenant,
    *,
    dry_run: bool = False,
    jobs: list | None = None,
) -> dict:
    """Remove duplicate cron jobs from a tenant's container.

    Groups jobs by name, keeps the newest (by createdAt), deletes the rest.

    Args:
        tenant: The tenant whose container to dedup.
        dry_run: If True, report duplicates without deleting.
        jobs: Pre-fetched job list (skips cron.list call if provided).

    Returns:
        {"kept": int, "deleted": int, "errors": int, "duplicates": list[dict]}
    """
    if jobs is None:
        try:
            list_result = invoke_gateway_tool(
                tenant, "cron.list", {"includeDisabled": True},
            )
        except GatewayError:
            logger.exception("dedup: failed to list crons for tenant %s", str(tenant.id)[:8])
            return {"kept": 0, "deleted": 0, "errors": 1, "duplicates": []}

        jobs = _extract_cron_jobs(list_result)
        if jobs is None:
            logger.warning(
                "dedup: tenant %s — could not parse cron.list response, skipping. "
                "Raw response: %s",
                str(tenant.id)[:8],
                repr(list_result)[:300],
            )
            return {"kept": 0, "deleted": 0, "errors": 1, "duplicates": []}

        logger.info(
            "dedup: tenant %s — found %d jobs to check",
            str(tenant.id)[:8], len(jobs),
        )

    # Group by name
    by_name: dict[str, list[dict]] = {}
    for job in jobs:
        name = job.get("name", "")
        if not name:
            continue
        by_name.setdefault(name, []).append(job)

    to_delete: list[dict] = []
    for name, group in by_name.items():
        if len(group) <= 1:
            continue
        # Sort by createdAt descending — keep the newest
        group.sort(
            key=lambda j: j.get("createdAt", j.get("id", "")),
            reverse=True,
        )
        for dupe in group[1:]:
            to_delete.append(dupe)

    if dry_run or not to_delete:
        return {
            "kept": len(by_name),
            "deleted": 0,
            "errors": 0,
            "duplicates": to_delete,
        }

    deleted = 0
    errors = 0
    for dupe in to_delete:
        job_id = dupe.get("id") or dupe.get("jobId", "")
        if not job_id:
            continue
        try:
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
            deleted += 1
        except GatewayError:
            logger.warning(
                "dedup: failed to delete job %s for tenant %s",
                job_id[:12], str(tenant.id)[:8],
            )
            errors += 1

    logger.info(
        "dedup: tenant %s — kept %d unique, deleted %d duplicates, %d errors",
        str(tenant.id)[:8], len(by_name), deleted, errors,
    )
    return {"kept": len(by_name), "deleted": deleted, "errors": errors, "duplicates": to_delete}


def _extract_cron_jobs(list_result) -> list | None:
    """Extract job list from a cron.list gateway response.

    Returns:
        list: The jobs list (may be empty for a fresh container).
        None: If the response format is unrecognizable (refuse to seed).
    """
    if isinstance(list_result, list):
        return list_result
    if isinstance(list_result, dict):
        # Gateway wraps in {"details": {"jobs": [...]}} or {"jobs": [...]}
        inner = list_result.get("details", list_result)
        if isinstance(inner, dict):
            jobs = inner.get("jobs")
            if isinstance(jobs, list):
                return jobs
        jobs = list_result.get("jobs")
        if isinstance(jobs, list):
            return jobs
    return None  # Unrecognizable — do NOT assume empty


SYSTEM_JOB_NAMES = frozenset({
    "Morning Briefing", "Evening Check-in", "Week Ahead Review",
    "Background Tasks", "Weekly Reflection", "Heartbeat Check-in",
})


def restore_user_cron_jobs(tenant: Tenant, existing_job_names: set[str]) -> dict:
    """Restore user-created cron jobs from the PostgreSQL snapshot.

    Called after seeding/reseeding when user jobs may have been lost
    due to a container restart wiping the in-memory SQLite.

    Returns: {"restored": int, "errors": int}
    """
    snapshot = getattr(tenant, "cron_jobs_snapshot", None)
    if not snapshot or not isinstance(snapshot, dict):
        return {"restored": 0, "errors": 0}

    snapshot_jobs = snapshot.get("jobs", [])
    if not snapshot_jobs:
        return {"restored": 0, "errors": 0}

    existing_lower = {n.lower() for n in existing_job_names}
    user_jobs_to_restore = [
        job for job in snapshot_jobs
        if isinstance(job, dict)
        and job.get("name")
        and job["name"] not in SYSTEM_JOB_NAMES
        and job["name"].lower() not in existing_lower
    ]

    # Deduplicate within snapshot — only restore one entry per name.
    # Dirty snapshots (saved before the tenant_views dedup fix) may
    # contain multiple entries with the same name.
    seen_names: set[str] = set()
    unique_jobs: list[dict] = []
    for job in user_jobs_to_restore:
        lower_name = job["name"].lower()
        if lower_name not in seen_names:
            seen_names.add(lower_name)
            unique_jobs.append(job)
    user_jobs_to_restore = unique_jobs

    if not user_jobs_to_restore:
        return {"restored": 0, "errors": 0}

    snapshot_at = snapshot.get("snapshot_at", "unknown")
    logger.info(
        "Restoring %d user cron jobs for tenant %s from snapshot at %s",
        len(user_jobs_to_restore), str(tenant.id)[:8], snapshot_at,
    )

    restored = 0
    errors = 0
    for job in user_jobs_to_restore:
        # Strip gateway-internal fields that cron.add rejects
        _STRIP_FIELDS = {"id", "jobId", "createdAt", "state", "createdAtMs", "updatedAtMs", "nextRunAtMs", "runningAtMs"}
        clean_job = {k: v for k, v in job.items() if k not in _STRIP_FIELDS}
        try:
            invoke_gateway_tool(tenant, "cron.add", {"job": clean_job})
            restored += 1
        except GatewayError as exc:
            logger.warning(
                "Failed to restore cron job '%s' for tenant %s: %s",
                job.get("name"), str(tenant.id)[:8], exc,
            )
            errors += 1

    return {"restored": restored, "errors": errors}


def seed_cron_jobs(tenant: Tenant | str) -> dict:
    """Seed default cron jobs for a tenant through the Gateway API."""
    if isinstance(tenant, str):
        tenant = Tenant.objects.select_related("user").get(id=tenant)

    tenant_id = str(tenant.id)
    jobs = build_cron_seed_jobs(tenant)

    if _is_mock():
        logger.info("[MOCK] seed_cron_jobs for tenant %s (%d jobs)", tenant_id, len(jobs))
        return {
            "tenant_id": tenant_id,
            "jobs_total": len(jobs),
            "created": len(jobs),
            "errors": 0,
        }

    # Check existing jobs first with retry on transient gateway failures.
    list_result = None
    for attempt in range(1, 4):
        try:
            list_result = invoke_gateway_tool(
                tenant,
                "cron.list",
                {"includeDisabled": True},
            )
            break
        except GatewayError as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code in (502, 503, 504) and attempt < 3:
                logger.warning(
                    "Transient failure checking cron jobs for tenant %s (attempt %d/3): %s",
                    tenant_id,
                    attempt,
                    exc,
                )
                time.sleep(10)
                continue
            raise

    if list_result is None:
        raise RuntimeError(f"Failed to list cron jobs for tenant {tenant_id}")

    existing_jobs = _extract_cron_jobs(list_result)

    # If we got a valid response (even empty list), trust it.
    # If we got None (unparseable response), refuse to seed — safer to skip
    # than to create duplicates.
    if existing_jobs is None:
        logger.warning(
            "seed_cron_jobs: tenant %s — could not parse cron.list response, "
            "refusing to seed (would create duplicates). Response: %s",
            tenant_id,
            repr(list_result)[:200],
        )
        return {
            "tenant_id": tenant_id,
            "jobs_total": len(jobs),
            "created": 0,
            "errors": 0,
            "skipped": True,
            "reason": "unparseable_cron_list",
        }

    # Diff by name — only create jobs that don't already exist
    existing_names = {
        j.get("name", "").lower()
        for j in existing_jobs
        if isinstance(j, dict) and j.get("name")
    }
    jobs_to_create = [j for j in jobs if j.get("name", "").lower() not in existing_names]

    if not jobs_to_create:
        logger.info(
            "seed_cron_jobs: tenant %s already has all %d jobs, skipping",
            tenant_id,
            len(jobs),
        )
        return {
            "tenant_id": tenant_id,
            "jobs_total": len(jobs),
            "created": 0,
            "errors": 0,
            "skipped": True,
        }

    logger.info(
        "seed_cron_jobs: tenant %s has %d/%d jobs, creating %d missing",
        tenant_id,
        len(existing_names),
        len(jobs),
        len(jobs_to_create),
    )

    created = 0
    errors = 0
    for job in jobs_to_create:
        for attempt in range(1, 4):
            try:
                invoke_gateway_tool(tenant, "cron.add", {"job": job})
                created += 1
                break
            except GatewayError as exc:
                status_code = getattr(exc, "status_code", None)
                if status_code in (502, 503, 504) and attempt < 3:
                    logger.warning(
                        "Transient failure creating cron job for tenant %s (attempt %d/3): %s",
                        tenant_id,
                        attempt,
                        exc,
                    )
                    time.sleep(5)
                    continue
                errors += 1
                logger.warning(
                    "Failed to create cron job for tenant %s (attempt %d): %s",
                    tenant_id,
                    attempt,
                    exc,
                )
                break
            except Exception:
                errors += 1
                logger.exception("Failed to create cron job for tenant %s", tenant_id)
                break

    # Post-creation dedup pass — clean up any race-condition duplicates
    if created > 0:
        try:
            dedup_tenant_cron_jobs(tenant)
        except Exception:
            logger.warning(
                "seed_cron_jobs: post-creation dedup failed for tenant %s (non-fatal)",
                tenant_id,
                exc_info=True,
            )

    logger.info(
        "seed_cron_jobs: tenant %s -> created=%d errors=%d (total=%d)",
        tenant_id,
        created,
        errors,
        len(jobs),
    )

    # Restore user-created jobs from snapshot if any were lost
    user_restore = {"restored": 0, "errors": 0}
    try:
        post_seed_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        post_seed_jobs = _extract_cron_jobs(post_seed_result) or []
        post_seed_names = {j.get("name", "") for j in post_seed_jobs if isinstance(j, dict)}
        user_restore = restore_user_cron_jobs(tenant, post_seed_names)
        if user_restore["restored"] > 0:
            logger.info(
                "seed_cron_jobs: restored %d user jobs for tenant %s",
                user_restore["restored"], tenant_id,
            )
    except Exception:
        logger.warning(
            "seed_cron_jobs: user job restore failed for tenant %s (non-fatal)",
            tenant_id, exc_info=True,
        )

    # Safety-net dedup after restore — catch any duplicates introduced by restore
    if user_restore.get("restored", 0) > 0:
        try:
            dedup_tenant_cron_jobs(tenant)
        except Exception:
            logger.warning(
                "seed_cron_jobs: post-restore dedup failed for tenant %s (non-fatal)",
                tenant_id, exc_info=True,
            )

    return {
        "tenant_id": tenant_id,
        "jobs_total": len(jobs),
        "created": created,
        "errors": errors,
        "skipped_existing": len(existing_names),
        "user_jobs_restored": user_restore["restored"],
    }


def update_system_cron_prompts(tenant: Tenant | str) -> dict:
    """Update system cron jobs to match current config_generator.

    Only patches jobs where:
    - The prompt hasn't been customized by the user (matches a known default)
    - OR the schedule timezone is wrong (doesn't match user's current tz)

    Leaves user-customized prompts untouched. Skips jobs the user deleted.
    """
    if isinstance(tenant, str):
        tenant = Tenant.objects.select_related("user").get(id=tenant)

    tenant_id = str(tenant.id)
    desired_jobs = build_cron_seed_jobs(tenant)

    # Get existing jobs
    try:
        list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
    except GatewayError:
        logger.exception("update_system_cron_prompts: failed to list jobs for %s", tenant_id)
        return {"tenant_id": tenant_id, "updated": 0, "skipped": 0, "errors": 1}

    existing_jobs = []
    # Gateway wraps cron.list result in {"details": {"jobs": [...]}} — unwrap it.
    if isinstance(list_result, dict):
        inner = list_result.get("details", list_result)
        if isinstance(inner, dict):
            existing_jobs = inner.get("jobs", [])
        else:
            existing_jobs = list_result.get("jobs", [])
    elif isinstance(list_result, list):
        existing_jobs = list_result

    # Build name → job map from existing jobs
    existing_by_name: dict[str, dict] = {}
    for job in existing_jobs:
        name = job.get("name", "")
        if name:
            existing_by_name[name] = job

    # Known old default prompt prefixes — if an existing prompt starts with
    # one of these, the user hasn't customized it and we can safely update.
    # Add new entries here when changing default prompts.
    _KNOWN_DEFAULT_PREFIXES = [
        "Good morning! Create today's morning briefing",
        "Good morning! Create today's morning briefing. This is a cron",
        "It's evening check-in time.",
        "It's Monday morning. Run the Week Ahead Review",
        "Background maintenance run.",
        "You received a scheduled check-in.",
        # Date-injected variants (added 2026-03-08):
        "Current date and time:",

    ]

    def _is_default_prompt(existing_message: str) -> bool:
        """Return True if the existing prompt matches a known default (old or current)."""
        msg = existing_message.strip()
        return any(msg.startswith(prefix) for prefix in _KNOWN_DEFAULT_PREFIXES)

    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")

    updated = 0
    skipped = 0
    errors = 0
    for desired in desired_jobs:
        name = desired.get("name", "")
        if name not in existing_by_name:
            continue  # Job doesn't exist (deleted by user or not seeded)

        existing = existing_by_name[name]
        job_id = existing.get("id", "")
        if not job_id:
            continue

        # Check what needs updating
        patch: dict = {}

        # Check prompt: only update if it matches a known default
        existing_payload = existing.get("payload", {})
        existing_message = existing_payload.get("message", "")
        desired_payload = desired.get("payload", {})
        desired_message = desired_payload.get("message", "")

        if existing_message != desired_message:
            if _is_default_prompt(existing_message):
                patch["payload"] = desired_payload
            else:
                logger.info(
                    "update_system_cron_prompts: skipping '%s' for tenant %s (user-customized)",
                    name, tenant_id,
                )
                skipped += 1

        # Check timezone: always fix if wrong
        existing_schedule = existing.get("schedule", {})
        existing_tz = existing_schedule.get("tz", "UTC")
        if existing_tz != user_tz:
            patch["schedule"] = desired.get("schedule", {})
            logger.info(
                "update_system_cron_prompts: fixing tz '%s' -> '%s' for '%s' tenant %s",
                existing_tz, user_tz, name, tenant_id,
            )

        if not patch:
            continue  # Nothing to update

        try:
            invoke_gateway_tool(tenant, "cron.update", {"jobId": job_id, "patch": patch})
            updated += 1
            logger.info("update_system_cron_prompts: updated '%s' for tenant %s", name, tenant_id)
        except GatewayError:
            logger.exception("update_system_cron_prompts: failed to update '%s' for %s", name, tenant_id)
            errors += 1

    # --- Heartbeat add/remove drift correction ---
    sync_heartbeat_cron(tenant, existing_by_name)

    return {"tenant_id": tenant_id, "updated": updated, "skipped": skipped, "errors": errors}


def sync_heartbeat_cron(
    tenant: Tenant,
    existing_by_name: dict[str, dict] | None = None,
) -> str:
    """Ensure the Heartbeat Check-in cron job matches the tenant's settings.

    - heartbeat_enabled=True → job must exist (add if missing, update schedule if changed)
    - heartbeat_enabled=False → job must not exist (remove if present)

    ``existing_by_name`` is an optional pre-fetched {name: job} map to avoid
    a redundant cron.list call when called from update_system_cron_prompts.

    Returns: "added", "removed", "updated", "ok", or "error".
    """
    from .config_generator import _build_heartbeat_cron

    HEARTBEAT_NAME = "Heartbeat Check-in"

    if not tenant.container_fqdn:
        return "ok"

    # Fetch existing jobs if not provided
    if existing_by_name is None:
        try:
            list_result = invoke_gateway_tool(
                tenant, "cron.list", {"includeDisabled": True}
            )
        except GatewayError:
            logger.exception("sync_heartbeat_cron: cannot list jobs for %s", tenant.id)
            return "error"

        jobs = []
        if isinstance(list_result, dict):
            inner = list_result.get("details", list_result)
            if isinstance(inner, dict):
                jobs = inner.get("jobs", [])
            else:
                jobs = list_result.get("jobs", [])
        elif isinstance(list_result, list):
            jobs = list_result

        existing_by_name = {}
        for job in jobs:
            name = job.get("name", "")
            if name:
                existing_by_name[name] = job

    existing_hb = existing_by_name.get(HEARTBEAT_NAME)
    desired_hb = _build_heartbeat_cron(tenant)  # None if disabled

    try:
        if desired_hb and not existing_hb:
            # Heartbeat enabled but job missing → add it
            invoke_gateway_tool(tenant, "cron.add", {"job": desired_hb})
            logger.info("sync_heartbeat_cron: added heartbeat for tenant %s", tenant.id)
            return "added"

        if not desired_hb and existing_hb:
            # Heartbeat disabled but job exists → remove it
            job_id = existing_hb.get("id") or existing_hb.get("jobId", HEARTBEAT_NAME)
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
            logger.info("sync_heartbeat_cron: removed heartbeat for tenant %s", tenant.id)
            return "removed"

        if desired_hb and existing_hb:
            # Both exist — check if schedule needs updating
            existing_expr = existing_hb.get("schedule", {}).get("expr", "")
            desired_expr = desired_hb["schedule"]["expr"]
            existing_tz = existing_hb.get("schedule", {}).get("tz", "UTC")
            desired_tz = desired_hb["schedule"]["tz"]

            if existing_expr != desired_expr or existing_tz != desired_tz:
                job_id = existing_hb.get("id") or existing_hb.get("jobId", HEARTBEAT_NAME)
                invoke_gateway_tool(
                    tenant, "cron.update",
                    {"jobId": job_id, "patch": {"schedule": desired_hb["schedule"]}},
                )
                logger.info(
                    "sync_heartbeat_cron: updated schedule for tenant %s (%s → %s)",
                    tenant.id, existing_expr, desired_expr,
                )
                return "updated"

    except GatewayError:
        logger.exception("sync_heartbeat_cron: failed for tenant %s", tenant.id)
        return "error"

    return "ok"


def check_tenant_health(tenant_id: str) -> dict:
    """Check if a tenant's OpenClaw instance is healthy."""
    tenant = Tenant.objects.get(id=tenant_id)

    if not tenant.container_fqdn:
        return {"tenant_id": str(tenant.id), "healthy": False, "reason": "no container"}

    # TODO: Actually ping the OpenClaw gateway health endpoint
    # For now, return based on status
    return {
        "tenant_id": str(tenant.id),
        "healthy": tenant.status == Tenant.Status.ACTIVE,
        "status": tenant.status,
        "container": tenant.container_id,
    }
