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
    store_tenant_internal_key_in_key_vault,
    upload_config_to_file_share,
)
from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
from .key_utils import generate_internal_api_key, hash_internal_api_key
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

        # 2c. Generate per-tenant internal API key
        plaintext_key = generate_internal_api_key()
        key_hash = hash_internal_api_key(plaintext_key)

        # 2d. Store plaintext in Key Vault
        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="store_internal_key")
        kv_secret_name = store_tenant_internal_key_in_key_vault(
            str(tenant.id), plaintext_key,
        )

        # 2e. Store hash in DB
        tenant.internal_api_key_hash = key_hash
        tenant.internal_api_key_set_at = timezone.now()
        tenant.save(update_fields=[
            "internal_api_key_hash", "internal_api_key_set_at", "updated_at",
        ])

        # 2f. Create Azure File Share and register with Container Environment
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
            internal_api_key_kv_secret=kv_secret_name,
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
        from .personas import render_workspace_files

        persona_key = (tenant.user.preferences or {}).get("agent_persona", "neighbor")
        workspace_files = render_workspace_files(persona_key, tenant=tenant)

        file_map = {
            "NBHD_AGENTS_MD": "workspace/AGENTS.md",
            "NBHD_SOUL_MD": "workspace/SOUL.md",
            "NBHD_IDENTITY_MD": "workspace/IDENTITY.md",
        }
        for env_key, file_path in file_map.items():
            content = workspace_files.get(env_key, "")
            if content:
                upload_workspace_file(str(tenant.id), file_path, content)
    except Exception:
        logger.exception("Failed to upload workspace files for tenant %s", tenant_id)

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
        tenant.internal_api_key_hash = ""
        tenant.internal_api_key_set_at = None
        tenant.save(update_fields=[
            "status", "container_id", "container_fqdn",
            "managed_identity_id", "internal_api_key_hash",
            "internal_api_key_set_at", "updated_at",
        ])

        logger.info("Deprovisioned tenant %s", tenant_id)

    except Exception:
        logger.exception("Failed to deprovision tenant %s", tenant_id)
        tenant.status = Tenant.Status.SUSPENDED
        tenant.save(update_fields=["status", "updated_at"])
        raise


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

    existing_jobs = []
    if isinstance(list_result, dict) and isinstance(list_result.get("jobs", []), list):
        existing_jobs = list_result.get("jobs", [])
    elif isinstance(list_result, list):
        existing_jobs = list_result

    if existing_jobs:
        logger.info(
            "seed_cron_jobs: tenant %s already has %d jobs, skipping seed",
            tenant_id,
            len(existing_jobs),
        )
        return {
            "tenant_id": tenant_id,
            "jobs_total": len(jobs),
            "created": 0,
            "errors": 0,
            "skipped": True,
        }

    created = 0
    errors = 0
    for job in jobs:
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

    logger.info(
        "seed_cron_jobs: tenant %s -> created=%d errors=%d (total=%d)",
        tenant_id,
        created,
        errors,
        len(jobs),
    )

    return {
        "tenant_id": tenant_id,
        "jobs_total": len(jobs),
        "created": created,
        "errors": errors,
    }


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
