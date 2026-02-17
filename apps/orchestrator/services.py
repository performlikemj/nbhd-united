"""Orchestrator services — provision/deprovision OpenClaw instances."""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.tenants.models import Tenant
from .azure_client import (
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
    update_container_env_var,
    upload_config_to_file_share,
)
from .key_utils import generate_internal_api_key, hash_internal_api_key
from .config_generator import build_cron_seed_jobs, config_to_json, generate_openclaw_config
from .personas import render_templates_md
from .personas import render_workspace_files

logger = logging.getLogger(__name__)


def provision_tenant(tenant_id: str) -> None:
    """Full provisioning flow for a new tenant."""
    tenant = Tenant.objects.select_related("user").get(id=tenant_id)
    secret_backend = str(
        getattr(settings, "OPENCLAW_CONTAINER_SECRET_BACKEND", "keyvault") or "keyvault"
    ).strip().lower()

    if tenant.status not in (Tenant.Status.PENDING, Tenant.Status.PROVISIONING):
        logger.warning("Tenant %s in unexpected state %s for provisioning", tenant_id, tenant.status)
        return

    tenant.status = Tenant.Status.PROVISIONING
    tenant.save(update_fields=["status", "updated_at"])

    try:
        # 1. Generate OpenClaw config
        config = generate_openclaw_config(tenant)
        config_json = config_to_json(config)

        # 2. Create Managed Identity
        identity = create_managed_identity(str(tenant.id))

        # 2b. Grant identity Key Vault access for secret references (keyvault backend only)
        if secret_backend == "keyvault":
            assign_key_vault_role(identity["principal_id"])

        # 2b2. Grant identity ACR pull access for container image
        assign_acr_pull_role(identity["principal_id"])

        # 2c. Generate per-tenant internal API key
        plaintext_key = generate_internal_api_key()
        key_hash = hash_internal_api_key(plaintext_key)

        # 2d. Store plaintext in Key Vault
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
        create_tenant_file_share(str(tenant.id))
        register_environment_storage(str(tenant.id))

        # 2f2. Write config to file share so OpenClaw reads it on first boot
        upload_config_to_file_share(str(tenant.id), config_json)

        # 2g. Render workspace templates based on persona
        persona_key = (tenant.user.preferences or {}).get("agent_persona", "neighbor")
        workspace_env = render_workspace_files(persona_key, tenant=tenant)

        # 3. Create Container App
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
        tenant.status = Tenant.Status.ACTIVE
        tenant.provisioned_at = timezone.now()
        tenant.save(update_fields=[
            "container_id", "container_fqdn", "managed_identity_id",
            "status", "provisioned_at", "updated_at",
        ])

        logger.info("Provisioned tenant %s → container %s", tenant_id, result["name"])

        # 5. Seed default cron jobs to file share (loaded by Gateway on boot)
        try:
            seed_cron_jobs(tenant)
        except Exception:
            logger.warning(
                "Could not seed cron jobs for tenant %s",
                tenant_id,
                exc_info=True,
            )

    except Exception:
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

    # Also update env var for consistency (used on first-ever boot of new revisions)
    update_container_env_var(
        tenant.container_id, "OPENCLAW_CONFIG_JSON", config_json,
    )

    # Push tenant-specific skill templates
    try:
        templates_md = render_templates_md(tenant)
        update_container_env_var(
            tenant.container_id, "NBHD_SKILL_TEMPLATES_MD", templates_md,
        )
    except Exception:
        logger.warning("Failed to update skill templates for tenant %s", tenant_id, exc_info=True)

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
    """Seed default cron job definitions into a tenant's workspace file share.

    Writes ``cron/jobs.json`` to the Azure File Share so the Gateway loads
    them on startup.  Only writes if the file is empty (no existing jobs),
    preserving any user modifications.
    """
    import json as _json

    from .azure_client import get_storage_client, _is_mock

    if isinstance(tenant, str):
        tenant = Tenant.objects.select_related("user").get(id=tenant)

    tenant_id = str(tenant.id)
    share_name = f"ws-{tenant_id[:20]}"
    jobs = build_cron_seed_jobs(tenant)

    if _is_mock():
        logger.info("[MOCK] seed_cron_jobs for tenant %s (%d jobs)", tenant_id, len(jobs))
        return {"tenant_id": tenant_id, "jobs_total": len(jobs), "created": len(jobs), "errors": 0}

    account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
    if not account_name:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not configured")

    from azure.storage.fileshare import ShareFileClient

    storage_client = get_storage_client()
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP, account_name,
    )
    account_key = keys.keys[0].value

    file_client = ShareFileClient(
        account_url=f"https://{account_name}.file.core.windows.net",
        share_name=share_name,
        file_path="cron/jobs.json",
        credential=account_key,
    )

    # Read existing jobs — only seed if the file is empty or missing.
    existing_jobs: list = []
    try:
        data = file_client.download_file().readall()
        parsed = _json.loads(data)
        existing_jobs = parsed.get("jobs", [])
    except Exception:
        pass  # File missing or unparseable — will create fresh

    if existing_jobs:
        logger.info(
            "seed_cron_jobs: tenant %s already has %d jobs, skipping seed",
            tenant_id, len(existing_jobs),
        )
        return {
            "tenant_id": tenant_id,
            "jobs_total": len(jobs),
            "created": 0,
            "errors": 0,
            "skipped": True,
        }

    jobs_json = _json.dumps({"version": 1, "jobs": jobs}, indent=2)
    data = jobs_json.encode("utf-8")
    file_client.upload_file(data, length=len(data))
    logger.info("seed_cron_jobs: wrote %d jobs to %s/cron/jobs.json", len(jobs), share_name)

    return {
        "tenant_id": tenant_id,
        "jobs_total": len(jobs),
        "created": len(jobs),
        "errors": 0,
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
