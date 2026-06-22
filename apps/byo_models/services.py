"""Service layer for BYO subscription credentials.

Mirrors `apps.integrations.services` patterns: write to Key Vault, store
the secret name in Postgres, never store the token value itself. The
container reads tokens at boot via env-var-mapped KV references, so
Django never needs to read tokens back.
"""

from __future__ import annotations

import logging
import os

from django.db import transaction

from apps.byo_models.models import BYOCredential
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Mock store for tests (AZURE_MOCK=true). Each test should reset by
# importing this dict and clearing it, or by using @override_settings.
_BYO_MOCK_KV_STORE: dict[str, str] = {}


def _is_mock() -> bool:
    return os.environ.get("AZURE_MOCK", "false").lower() == "true"


def secret_name_for(tenant: Tenant, provider: str, mode: str) -> str:
    """Build the Key Vault secret name for a BYO credential.

    Naming: `<tenant.key_vault_prefix>-byo-<provider>-<sanitized-mode>`.
    Mirrors `apps.integrations.services.get_key_vault_secret_name` but
    with a `byo-` prefix and explicit mode component to disambiguate
    from OAuth integration tokens (which use `<prefix>-<provider>-token`).

    Azure Key Vault rejects secret names with characters outside
    `^[0-9a-zA-Z-]+$`. The DB stores enum values like `cli_subscription`
    (with an underscore), so we replace `_` → `-` here. Sanitization is
    one-way and stable; the DB value is unchanged.
    """
    if not tenant.key_vault_prefix:
        raise ValueError(
            f"Tenant {tenant.id} has no key_vault_prefix — cannot build BYO secret name for {provider}/{mode}"
        )
    safe_mode = mode.replace("_", "-")
    return f"{tenant.key_vault_prefix}-byo-{provider}-{safe_mode}"


def _write_secret_to_kv(secret_name: str, value: str) -> None:
    """Persist a single string value to Key Vault.

    The token never appears in logs (we log only the secret_name) and
    never flows back to Django after writing.

    Auto-recovers from KV soft-delete: if a prior `delete_credential`
    soft-deleted the same name and the 7–90 day retention window hasn't
    elapsed, `set_secret` would 409 with `ObjectIsDeletedButRecoverable`.
    We recover the deleted secret (which restores it with its old value)
    and immediately overwrite with the new value.
    """
    if _is_mock():
        _BYO_MOCK_KV_STORE[secret_name] = value
        logger.info("[MOCK] Wrote BYO secret %s", secret_name)
        return

    from azure.core.exceptions import ResourceExistsError
    from azure.keyvault.secrets import SecretClient
    from django.conf import settings

    from apps.orchestrator.azure_client import _get_provisioner_credential

    vault_url = f"https://{settings.AZURE_KEY_VAULT_NAME}.vault.azure.net"
    client = SecretClient(vault_url=vault_url, credential=_get_provisioner_credential())
    try:
        client.set_secret(secret_name, value)
    except ResourceExistsError as exc:
        if "ObjectIsDeletedButRecoverable" not in str(exc):
            raise
        logger.info("BYO secret %s is soft-deleted; recovering before overwrite", secret_name)
        recover_poller = client.begin_recover_deleted_secret(secret_name)
        recover_poller.wait()
        client.set_secret(secret_name, value)
    logger.info("Wrote BYO secret %s", secret_name)


def _delete_secret_from_kv(secret_name: str) -> None:
    """Soft-delete a Key Vault secret. KV soft-delete is enabled by default.

    Idempotent: errors during delete (e.g. secret already gone) are
    logged and swallowed — caller still proceeds to delete the row.
    """
    if _is_mock():
        _BYO_MOCK_KV_STORE.pop(secret_name, None)
        logger.info("[MOCK] Deleted BYO secret %s", secret_name)
        return

    from azure.keyvault.secrets import SecretClient
    from django.conf import settings

    from apps.orchestrator.azure_client import _get_provisioner_credential

    vault_url = f"https://{settings.AZURE_KEY_VAULT_NAME}.vault.azure.net"
    client = SecretClient(vault_url=vault_url, credential=_get_provisioner_credential())
    try:
        poller = client.begin_delete_secret(secret_name)
        poller.wait()
        logger.info("Deleted BYO secret %s", secret_name)
    except Exception:
        logger.exception("Failed to delete BYO secret %s", secret_name)


def upsert_credential(
    tenant: Tenant,
    provider: str,
    mode: str,
    token: str,
) -> BYOCredential:
    """Write the token to Key Vault and upsert the BYOCredential row.

    Atomic at the DB level — KV write happens before the row update so
    a KV failure leaves no orphaned row. Caller is responsible for
    triggering a new container revision after this returns.
    """
    secret_name = secret_name_for(tenant, provider, mode)
    _write_secret_to_kv(secret_name, token)

    with transaction.atomic():
        cred, _created = BYOCredential.objects.update_or_create(
            tenant=tenant,
            provider=provider,
            defaults={
                "mode": mode,
                "key_vault_secret_name": secret_name,
                "status": BYOCredential.Status.PENDING,
                "last_verified_at": None,
                "last_error": "",
            },
        )
        # Bump seed_version on every paste/re-paste. Phase 2 Codex
        # entrypoint compares this against an on-disk marker.
        BYOCredential.objects.filter(pk=cred.pk).update(
            seed_version=cred.seed_version + 1,
        )
        cred.refresh_from_db()
    return cred


def delete_credential(cred: BYOCredential) -> None:
    """Soft-delete the KV secret and remove the BYOCredential row."""
    secret_name = cred.key_vault_secret_name
    cred_id = cred.id
    cred.delete()
    _delete_secret_from_kv(secret_name)
    logger.info("Deleted BYOCredential %s (secret=%s)", cred_id, secret_name)


# Maximum stored length for `last_error` — the field is TextField but a
# raw provider blob may be very large. Truncate so the rose banner stays
# readable in `BYOProviderCard`.
_LAST_ERROR_MAX_LEN = 240


def mark_credential_error(
    tenant: Tenant,
    provider: str,
    last_error: str,
) -> BYOCredential | None:
    """Flip a tenant's BYO credential into the `error` state with a
    user-facing message.

    Used by the runtime when OpenClaw reports a billing/auth failure
    against the BYO route (e.g. claude-binary returns
    `"out of extra usage"`). The frontend `BYOProviderCard` already
    surfaces `last_error` in a rose banner when status is `error`,
    so this is the join point that closes the loop between "claude
    failed" → "user sees what to do".

    Returns the updated row, or None if no matching credential exists.
    Idempotent: safe to call repeatedly with the same message.
    """
    cleaned = (last_error or "").strip()
    if len(cleaned) > _LAST_ERROR_MAX_LEN:
        cleaned = cleaned[: _LAST_ERROR_MAX_LEN - 1].rstrip() + "…"
    # Flip whatever live credential exists into ERROR. We must NOT exclude
    # PENDING: in Phase 1 there is no background verifier, so a real working
    # credential stays PENDING forever (the container applies it regardless),
    # and excluding PENDING made this never match a live cred — the entire
    # error-surfacing loop was dead in prod. Exclude only ERROR so repeated
    # runtime reports are idempotent no-ops.
    cred = (
        BYOCredential.objects.filter(tenant=tenant, provider=provider)
        .exclude(status=BYOCredential.Status.ERROR)
        .first()
    )
    if cred is None:
        return None
    BYOCredential.objects.filter(pk=cred.pk).update(
        status=BYOCredential.Status.ERROR,
        last_error=cleaned,
    )
    cred.refresh_from_db()
    logger.info(
        "Marked BYOCredential %s (tenant=%s, provider=%s) as error: %s",
        cred.id,
        tenant.id,
        provider,
        cleaned[:80],
    )
    return cred


def regenerate_tenant_config(tenant: Tenant) -> None:
    """Synchronously regenerate the tenant's openclaw.json on the file
    share and advance `config_version` so the apply-pending-configs cron
    doesn't re-process this transition.

    Mirrors `apps.orchestrator.tasks.apply_single_tenant_config_task`:
    write the regenerated config to the share, stamp ``applied_model``,
    and advance ``config_version``. The new config takes effect on the
    next container revision restart — BYO flows always trigger one right
    after this via ``apply_byo_credentials_to_container``.

    No-op for tenants without a container_id or in a non-active status,
    since the underlying `update_tenant_config` would bail anyway.
    """
    from django.db import models as db_models
    from django.utils import timezone as tz

    from apps.orchestrator.services import update_tenant_config

    if _is_mock():
        logger.info("[MOCK] regenerate_tenant_config for tenant=%s", tenant.id)
        return

    update_tenant_config(str(tenant.id))
    # Stamp applied_model in lockstep with config_version. The BYO
    # connect/disconnect that called this is about to restart the
    # revision, which is when the new config actually loads.
    Tenant.objects.filter(id=tenant.id).update(
        config_version=db_models.F("pending_config_version"),
        config_refreshed_at=tz.now(),
        applied_model=tenant.preferred_model,
        applied_model_at=tz.now(),
    )
    logger.info("Regenerated openclaw.json for tenant=%s", tenant.id)
