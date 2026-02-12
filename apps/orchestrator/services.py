"""Orchestrator services — provision/deprovision OpenClaw instances."""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.tenants.models import Tenant
from .azure_client import (
    assign_key_vault_role,
    create_container_app,
    create_managed_identity,
    delete_container_app,
    delete_managed_identity,
    store_tenant_internal_key_in_key_vault,
)
from .key_utils import generate_internal_api_key, hash_internal_api_key
from .config_generator import config_to_json, generate_openclaw_config

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

        # 3. Create Container App
        container_name = f"oc-{str(tenant.id)[:20]}"
        result = create_container_app(
            tenant_id=str(tenant.id),
            container_name=container_name,
            config_json=config_json,
            identity_id=identity["id"],
            identity_client_id=identity["client_id"],
            internal_api_key_kv_secret=kv_secret_name,
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

    except Exception:
        logger.exception("Failed to provision tenant %s", tenant_id)
        tenant.status = Tenant.Status.PENDING
        tenant.save(update_fields=["status", "updated_at"])
        raise


def deprovision_tenant(tenant_id: str) -> None:
    """Full deprovisioning flow."""
    tenant = Tenant.objects.get(id=tenant_id)

    tenant.status = Tenant.Status.DEPROVISIONING
    tenant.save(update_fields=["status", "updated_at"])

    try:
        # 1. Delete container
        if tenant.container_id:
            delete_container_app(tenant.container_id)

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
