"""Orchestrator services — provision/deprovision OpenClaw instances."""

from __future__ import annotations

import logging
import secrets as secrets_lib
import time

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
from apps.tenants.models import Tenant

from .azure_client import (
    DEFAULT_TENANT_KV_SECRETS,
    _is_mock,
    assign_acr_pull_role,
    assign_key_vault_role,
    create_container_app,
    create_managed_identity,
    create_tenant_file_share,
    delete_container_app,
    delete_managed_identity,
    delete_tenant_file_share,
    download_config_from_file_share,
    register_environment_storage,
    store_tenant_internal_key_in_key_vault,
    update_container_image,
    upload_config_to_file_share,
)
from .config_generator import build_cron_seed_jobs, config_to_json, generate_openclaw_config
from .config_security import audit_config_security
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


def _audit_and_log(tenant: Tenant, config: dict, *, stage: str) -> None:
    """Run security audit on config and log findings to PlatformIssueLog.

    Warnings are logged but don't block. Errors are logged and raise.
    """
    findings = audit_config_security(config)
    if not findings:
        return

    from apps.platform_logs.models import PlatformIssueLog

    for finding in findings:
        severity_map = {"error": PlatformIssueLog.Severity.HIGH, "warning": PlatformIssueLog.Severity.MEDIUM}
        PlatformIssueLog.objects.create(
            tenant=tenant,
            category=PlatformIssueLog.Category.CONFIG_ISSUE,
            severity=severity_map.get(finding.severity, PlatformIssueLog.Severity.LOW),
            summary=f"[{stage}] {finding.check}: {finding.message}"[:500],
            tool_name="config_security_audit",
        )

    errors = [f for f in findings if f.severity == "error"]
    if errors:
        msg = f"Config security audit failed for tenant {tenant.id} ({stage}): {len(errors)} error(s)"
        logger.error(msg)
        for e in errors:
            logger.error("  %s: %s", e.check, e.message)
        raise ValueError(msg)

    warnings = [f for f in findings if f.severity == "warning"]
    if warnings:
        logger.warning(
            "Config security audit warnings for tenant %s (%s): %d warning(s)",
            tenant.id,
            stage,
            len(warnings),
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
            summary["results"].append(
                {
                    "tenant_id": tenant_id_str,
                    "user_id": user_id_str,
                    "status": tenant.status,
                    "result": "dry_run",
                    "missing": missing,
                }
            )
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

            summary["results"].append(
                {
                    "tenant_id": tenant_id_str,
                    "user_id": user_id_str,
                    "status": tenant.status,
                    "result": outcome,
                    "missing": [
                        field
                        for field, value in (
                            ("container_id", tenant.container_id),
                            ("container_fqdn", tenant.container_fqdn),
                        )
                        if not value
                    ],
                }
            )
        except Exception as exc:
            summary["failed"] += 1
            _log_provisioning_event(
                tenant_id=tenant_id_str,
                user_id=user_id_str,
                stage="repair_failed",
                error=str(exc),
            )
            summary["results"].append(
                {
                    "tenant_id": tenant_id_str,
                    "user_id": user_id_str,
                    "status": tenant.status,
                    "result": "failed",
                    "error": str(exc),
                    "missing": missing,
                }
            )

    return summary


def provision_tenant(tenant_id: str) -> None:
    """Full provisioning flow for a new tenant."""
    tenant = Tenant.objects.select_related("user").get(id=tenant_id)
    user_id = str(tenant.user_id)
    _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="provision_start")
    secret_backend = (
        str(getattr(settings, "OPENCLAW_CONTAINER_SECRET_BACKEND", "keyvault") or "keyvault").strip().lower()
    )

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

        # 1b. Security audit — log findings, block on errors
        _audit_and_log(tenant, config, stage="provision")

        # 2. Create Managed Identity
        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="create_managed_identity")
        identity = create_managed_identity(str(tenant.id))

        # 2a. (Phase 1b) Generate per-tenant internal API key, write to Key
        # Vault as `tenant-<uuid>-internal-key`, and stash on the Tenant
        # row so Django's `validate_internal_runtime_request` will accept
        # it as the per-tenant key. Closes the Django-side cross-tenant
        # pivot — a compromised tenant A can no longer call
        # /api/.../<tenant_B>/... with a leaked key. See PR #524 for the
        # validator dual-validation, then PR #525-something for fleet
        # migration of existing tenants.
        _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="generate_per_tenant_internal_key")
        internal_api_key_plain = secrets_lib.token_urlsafe(48)
        internal_api_key_kv_secret_name: str | None = None
        if secret_backend == "keyvault":
            internal_api_key_kv_secret_name = store_tenant_internal_key_in_key_vault(
                str(tenant.id), internal_api_key_plain
            )
        tenant.internal_api_key = internal_api_key_plain
        tenant.save(update_fields=["internal_api_key", "updated_at"])

        # 2a2. (PR #1.6) Per-tenant OpenRouter sub-key. When the feature
        # flag is on AND the management key is configured, create a
        # dedicated OR sub-key with a server-side spending limit. OR
        # enforces the per-tenant cap so we don't rely solely on our
        # internal estimate. The key string goes to KV; the hash is
        # saved on the Tenant row so deprovision can DELETE it later.
        # Failure here MUST NOT block provisioning — fall back to the
        # shared key with a warning.
        openrouter_kv_secret_name: str | None = None
        if tenant.openrouter_key_secret_name:
            # Idempotent re-entry guard: provision_tenant is re-runnable and
            # QStash retries it up to 3x. If the tenant is already keyed, reuse
            # the existing sub-key — re-minting would orphan the prior key (its
            # own monthly OpenRouter spend ceiling) and overwrite the hash so
            # deprovision can never reap it. Mirrors backfill_openrouter_keys.
            openrouter_kv_secret_name = tenant.openrouter_key_secret_name
        elif getattr(settings, "OPENROUTER_PER_TENANT_KEYS_ENABLED", False) and secret_backend == "keyvault":
            try:
                from apps.billing.constants import TIER_COST_BUDGETS
                from apps.billing.openrouter_admin import (
                    OpenRouterAdminError,
                    create_sub_key,
                    secret_name_for_tenant,
                )
                from apps.byo_models.services import _write_secret_to_kv

                tier = tenant.model_tier or "starter"
                limit = float(TIER_COST_BUDGETS.get(tier, 5.00))
                label = f"tenant-{str(tenant.id)[:8]}"

                _log_provisioning_event(
                    tenant_id=str(tenant.id),
                    user_id=user_id,
                    stage="create_openrouter_subkey",
                )
                api_key, key_hash = create_sub_key(label, limit_dollars=limit, limit_reset="monthly")
                openrouter_kv_secret_name = secret_name_for_tenant(tenant)
                _write_secret_to_kv(openrouter_kv_secret_name, api_key)
                tenant.openrouter_key_secret_name = openrouter_kv_secret_name
                tenant.openrouter_key_hash = key_hash
                tenant.save(
                    update_fields=[
                        "openrouter_key_secret_name",
                        "openrouter_key_hash",
                        "updated_at",
                    ]
                )
            except OpenRouterAdminError as exc:
                logger.warning(
                    "OpenRouter sub-key creation failed for tenant %s; falling back to shared key: %s",
                    tenant_id,
                    exc,
                )
                openrouter_kv_secret_name = None
            except Exception:
                logger.warning(
                    "Unexpected error creating OpenRouter sub-key for tenant %s; falling back to shared key",
                    tenant_id,
                    exc_info=True,
                )
                openrouter_kv_secret_name = None

        # 2b. Grant identity Key Vault access for secret references (keyvault backend only)
        if secret_backend == "keyvault":
            _log_provisioning_event(tenant_id=str(tenant.id), user_id=user_id, stage="assign_key_vault_role")
            secret_names_to_grant = list(DEFAULT_TENANT_KV_SECRETS)
            if internal_api_key_kv_secret_name:
                secret_names_to_grant.append(internal_api_key_kv_secret_name)
            if openrouter_kv_secret_name:
                secret_names_to_grant.append(openrouter_kv_secret_name)
            assign_key_vault_role(identity["principal_id"], secret_names=secret_names_to_grant)

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
            internal_api_key_kv_secret_name=internal_api_key_kv_secret_name,
            internal_api_key_plain_value=internal_api_key_plain,
            openrouter_kv_secret_name=openrouter_kv_secret_name,
        )

        # 4. Update tenant record
        tenant.container_id = result["name"]
        tenant.container_fqdn = result["fqdn"]
        tenant.managed_identity_id = identity["id"]
        tenant.container_image_tag = getattr(settings, "OPENCLAW_IMAGE_TAG", "latest") or "latest"
        tenant.status = Tenant.Status.ACTIVE
        tenant.provisioned_at = timezone.now()
        tenant.save(
            update_fields=[
                "container_id",
                "container_fqdn",
                "managed_identity_id",
                "container_image_tag",
                "status",
                "provisioned_at",
                "updated_at",
            ]
        )

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

    # 4c. Send Day-0 welcome email. Web-signup tenants never get the
    # Telegram welcome above (chat_id is None) — the email is the only
    # nudge they get. Idempotent via welcome_email_sent_at; safe on
    # provisioning retries.
    try:
        from apps.tenants.emails import send_welcome_email

        send_welcome_email(tenant)
    except Exception:
        logger.warning("Could not send welcome email for tenant %s", tenant_id, exc_info=True)

    # 4d. Seed USER.md with the platform-managed envelope so the container
    # picks up profile + state on first boot. force=True bypasses debounce.
    try:
        from .workspace_envelope import push_user_md

        push_user_md(tenant, force=True)
    except Exception:
        logger.warning("Could not seed USER.md for tenant %s", tenant_id, exc_info=True)

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


def bump_openclaw_version_for_tenant(
    tenant: Tenant,
    target_version: str,
    image_tag: str,
    registry: str,
) -> None:
    """Atomically bump one tenant's OpenClaw version + config + image.

    Single-tenant primitive shared by:
      - the ``bump_openclaw_version`` management command (sequential CLI use)
      - the ``bump_openclaw_atomic_per_tenant_task`` QStash task (fleet
        fan-out via ``rollout-atomic-bump`` endpoint)

    Atomicity guarantees:
      - On image-push failure (after config write succeeded), the prior
        ``openclaw.json`` snapshot is restored to the file share so the
        old image — which is still the running revision — can still boot.
        Without this, a config-write-then-image-fail leaves the old image
        trying to read a new schema it doesn't recognize, putting the
        tenant in a crash loop.
      - **Restore does NOT fire on update_tenant_config failures** —
        that step's only "raise" path that survives the file write is
        the gateway.reload step (`update_system_cron_prompts`), which is
        a hot-reload signal to a running gateway. If the gateway can't
        reload (container booting, in crashloop, hibernated), the file
        write has already succeeded and the file is the source of truth
        on the next boot. Restoring at that point would UNDO the fix and
        re-trap the tenant in the crashloop. Caught on canary 2026-05-08:
        old code restored stale config when only gateway.reload returned
        404, putting canary back into the same boot failure it was in.
      - DB ``tenant.openclaw_version`` is rolled back on any exception.
      - ``container_image_tag`` and ``hibernated_at`` are only updated
        after image push succeeds, so they reflect the actual deployed
        state.

    Hibernated tenants are NOT skipped — Container Apps single-revision
    mode means ``update_container_image`` creates a new active revision
    that auto-wakes the container.

    Raises on any per-tenant failure so the caller can record it.
    """
    was_hibernated = bool(tenant.hibernated_at)
    old_version = tenant.openclaw_version

    # Snapshot current config — only used if image-push fails after config
    # write has succeeded. None means the tenant doesn't have a config yet
    # (fresh provision); the restore path skips when there's nothing to
    # restore.
    config_snapshot: bytes | None
    try:
        config_snapshot = download_config_from_file_share(str(tenant.id))
    except Exception:
        logger.warning(
            "Could not snapshot config for tenant %s before bump (proceeding without restore safety)",
            str(tenant.id)[:8],
            exc_info=True,
        )
        config_snapshot = None

    # 1. Set version so config generator produces version-correct output.
    tenant.openclaw_version = target_version
    tenant.save(update_fields=["openclaw_version"])

    try:
        # 2. Regenerate and push config + workspace files.
        #
        # Failure modes inside update_tenant_config:
        #   - file-share write fails → tmp+rename atomicity means file is
        #     unchanged (still the OLD config). No restore needed.
        #   - workspace files fail → openclaw.json is the NEW config but
        #     workspace is partial. Boot still works; later workspace
        #     writes will reconcile. No restore needed.
        #   - gateway.reload fails (404, timeout, hibernated) → file is
        #     the NEW config and gateway just didn't get the live signal.
        #     File is the source of truth on next boot. No restore needed
        #     — restoring would re-trap a booting/crashlooping tenant.
        #
        # In ALL of those cases the file-share state is correct for the
        # next boot. We do not restore on update_tenant_config failures.
        update_tenant_config(str(tenant.id))

        # 3. Update container image (creates new revision; in single-revision
        #    mode this auto-activates and wakes hibernated containers).
        image = f"{registry}/nbhd-openclaw:{image_tag}"
        try:
            update_container_image(tenant.container_id, image)
        except Exception:
            # File share now holds NEW schema but the OLD image is still
            # the active revision. Old image will fail to boot on next
            # restart reading a schema it doesn't recognize. Best-effort
            # restore to keep the old image bootable until an operator
            # retries the bump.
            if config_snapshot is not None:
                try:
                    upload_config_to_file_share(str(tenant.id), config_snapshot.decode("utf-8"))
                    logger.info(
                        "Restored prior openclaw.json for tenant %s after image-push failure",
                        str(tenant.id)[:8],
                    )
                except Exception:
                    logger.exception(
                        "Failed to restore prior openclaw.json for tenant %s — manual file-share fix may be needed",
                        str(tenant.id)[:8],
                    )
            raise

        # 4. Record image tag and clear hibernation flag if applicable.
        tenant.container_image_tag = image_tag
        update_fields = ["container_image_tag"]
        if was_hibernated:
            tenant.hibernated_at = None
            update_fields.append("hibernated_at")
        tenant.save(update_fields=update_fields)
    except Exception:
        # Roll back the version field. The image-push-failure restore (if
        # any) happened in the inner try/except above — see docstring for
        # why update_tenant_config failures do NOT trigger a restore.
        tenant.openclaw_version = old_version
        tenant.save(update_fields=["openclaw_version"])
        raise


def update_tenant_config(tenant_id: str) -> None:
    """Regenerate OpenClaw config and update the running container."""
    tenant = Tenant.objects.select_related("user").get(id=tenant_id)

    if tenant.status != Tenant.Status.ACTIVE or not tenant.container_id:
        logger.warning(
            "Cannot update config for tenant %s (status=%s, container=%s)",
            tenant_id,
            tenant.status,
            tenant.container_id,
        )
        return

    config = generate_openclaw_config(tenant)
    config_json = config_to_json(config)

    # Security audit — log findings before pushing config
    _audit_and_log(tenant, config, stage="config_update")

    # Write to file share (source of truth — OpenClaw reads from file after first boot)
    upload_config_to_file_share(str(tenant.id), config_json)

    # Write workspace files (AGENTS.md, SOUL.md, etc.) to file share
    # so updates propagate without needing container env var changes.
    try:
        from .azure_client import upload_workspace_file
        from .personas import render_workspace_files, render_workspace_rules

        persona_key = (tenant.user.preferences or {}).get("agent_persona", "neighbor")
        workspace_files = render_workspace_files(persona_key, tenant=tenant)

        # System-controlled files — always overwrite on config refresh.
        file_map_overwrite = {
            "NBHD_AGENTS_MD": "workspace/AGENTS.md",
            # Reference docs — written to workspace/docs/ and read on-demand
            "NBHD_DOC_TOOLS_REFERENCE": "workspace/docs/tools-reference.md",
            "NBHD_DOC_CHANNEL_FORMATTING": "workspace/docs/channel-formatting.md",
            "NBHD_DOC_CRON_MANAGEMENT": "workspace/docs/cron-management.md",
            "NBHD_DOC_ERROR_HANDLING": "workspace/docs/error-handling.md",
            "NBHD_DOC_PRIVACY_REDACTION": "workspace/docs/privacy-redaction.md",
            # Daily-journal skill templates.md — the canonical slug source the
            # prompt points the agent at. Re-uploaded on config refresh so a
            # post-provision default-template edit (which fires
            # update_tenant_config_task) actually reaches the running container
            # instead of leaving the boot-time env-var copy stale. Destination
            # must match entrypoint.sh's SKILL_TEMPLATES_DST.
            "NBHD_SKILL_TEMPLATES_MD": "workspace/skills/nbhd-managed/daily-journal/references/templates.md",
        }

        # Deploy full or silent platform guide based on feature_tips_enabled
        guide_key = "NBHD_DOC_PLATFORM_GUIDE" if tenant.feature_tips_enabled else "NBHD_DOC_PLATFORM_GUIDE_SILENT"
        file_map_overwrite[guide_key] = "workspace/docs/platform-guide.md"

        # Agent-owned after first seed — never overwrite. Mirrors the
        # `[ ! -f ]` guards in runtime/openclaw/entrypoint.sh: SOUL.md and
        # IDENTITY.md are seeded once at provision and then belong to the
        # tenant's assistant. Overwriting them here would silently wipe any
        # evolution the agent has done.
        file_map_seed_once = {
            "NBHD_SOUL_MD": "workspace/SOUL.md",
            "NBHD_IDENTITY_MD": "workspace/IDENTITY.md",
        }

        for env_key, file_path in file_map_overwrite.items():
            content = workspace_files.get(env_key, "")
            if content:
                upload_workspace_file(str(tenant.id), file_path, content)

        for env_key, file_path in file_map_seed_once.items():
            content = workspace_files.get(env_key, "")
            if content:
                upload_workspace_file(
                    str(tenant.id),
                    file_path,
                    content,
                    skip_if_exists=True,
                )

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

    # Refresh USER.md (platform-managed envelope region merged with any
    # agent-written content). force=True so config refresh always pushes
    # current state regardless of debounce window.
    try:
        from .workspace_envelope import push_user_md

        push_user_md(tenant, force=True)
    except Exception:
        logger.exception("Failed to refresh USER.md for tenant %s (non-fatal)", tenant_id)

    # Refresh the postgres CronJob rows for this tenant's system crons from
    # the current seed. The CronJob post_save signal triggers a debounced
    # ``regenerate_tenant_crons`` task that diffs postgres → OC and pushes
    # any drift via cron.remove + cron.add. We don't touch the gateway from
    # here — the reconciler is the single writer for system cron payload state.
    try:
        result = refresh_system_cron_rows_from_seed(tenant)
        if result["created"] or result["updated"]:
            logger.info(
                "Refreshed system cron rows for tenant %s — created=%d updated=%d preserved_custom=%d",
                tenant_id,
                result["created"],
                result["updated"],
                result["preserved_custom"],
            )
    except Exception:
        logger.exception("Failed to refresh system cron rows for tenant %s (non-fatal)", tenant_id)

    logger.info("Updated OpenClaw config for tenant %s", tenant_id)


def deprovision_tenant(tenant_id: str) -> None:
    """Full deprovisioning flow."""
    tenant = Tenant.objects.get(id=tenant_id)

    tenant.status = Tenant.Status.DEPROVISIONING
    tenant.save(update_fields=["status", "updated_at"])

    try:
        # 0. (PR #1.6) Delete the per-tenant OpenRouter sub-key + KV
        # secret first. Errors here log + continue — leftover sub-keys
        # are reaped by the sweep_orphan_openrouter_keys command, and a
        # KV secret without a container is harmless until the next
        # tenant happens to share the same key_vault_prefix (impossible
        # because the prefix embeds the user id).
        if tenant.openrouter_key_hash:
            try:
                from apps.billing.openrouter_admin import OpenRouterAdminError, delete_sub_key

                delete_sub_key(tenant.openrouter_key_hash)
            except OpenRouterAdminError as exc:
                logger.warning(
                    "OR sub-key delete failed for tenant %s (hash=%s); reap via sweeper: %s",
                    tenant_id,
                    tenant.openrouter_key_hash,
                    exc,
                )
            except Exception:
                logger.warning(
                    "Unexpected error deleting OR sub-key for tenant %s; reap via sweeper",
                    tenant_id,
                    exc_info=True,
                )
        if tenant.openrouter_key_secret_name:
            try:
                from apps.byo_models.services import _delete_secret_from_kv

                _delete_secret_from_kv(tenant.openrouter_key_secret_name)
            except Exception:
                logger.warning(
                    "OR KV secret delete failed for tenant %s",
                    tenant_id,
                    exc_info=True,
                )

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
        tenant.openrouter_key_secret_name = ""
        tenant.openrouter_key_hash = ""
        tenant.save(
            update_fields=[
                "status",
                "container_id",
                "container_fqdn",
                "managed_identity_id",
                "openrouter_key_secret_name",
                "openrouter_key_hash",
                "updated_at",
            ]
        )

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
                tenant,
                "cron.list",
                {"includeDisabled": True},
            )
        except GatewayError:
            logger.exception("dedup: failed to list crons for tenant %s", str(tenant.id)[:8])
            return {"kept": 0, "deleted": 0, "errors": 1, "duplicates": []}

        jobs = _extract_cron_jobs(list_result)
        if jobs is None:
            logger.warning(
                "dedup: tenant %s — could not parse cron.list response, skipping. Raw response: %s",
                str(tenant.id)[:8],
                repr(list_result)[:300],
            )
            return {"kept": 0, "deleted": 0, "errors": 1, "duplicates": []}

        logger.info(
            "dedup: tenant %s — found %d jobs to check",
            str(tenant.id)[:8],
            len(jobs),
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
        # Sort by createdAtMs descending — keep the newest. OpenClaw returns
        # ``createdAtMs`` (epoch milliseconds), not ``createdAt`` — the
        # earlier ``j.get("createdAt", ...)`` always missed and fell back
        # to ``j.get("id", "")`` (UUID lex order), so dedup kept an arbitrary
        # copy. This is the same correct sort used by the reconciler's
        # dedup pre-pass in ``cron_reconcile.regenerate_tenant_crons``.
        group.sort(
            key=lambda j: j.get("createdAtMs") or 0,
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
                job_id[:12],
                str(tenant.id)[:8],
            )
            errors += 1

    logger.info(
        "dedup: tenant %s — kept %d unique, deleted %d duplicates, %d errors",
        str(tenant.id)[:8],
        len(by_name),
        deleted,
        errors,
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


SYSTEM_JOB_NAMES = frozenset(
    {
        "Morning Briefing",
        "Evening Check-in",
        "Week Ahead Review",
        "Background Tasks",
        "Weekly Reflection",
        "Heartbeat Check-in",
    }
)

# Prefixes for system-generated cron jobs (Phase 2 sync, Fuel workout prep).
# These are auto-created alongside their parent jobs and should NOT be
# restored from snapshot as "user" jobs — they may use legacy payload/session
# formats incompatible with newer OpenClaw versions.
_SYSTEM_GENERATED_PREFIXES = ("_sync:", "_fuel:")


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
        job
        for job in snapshot_jobs
        if isinstance(job, dict)
        and job.get("name")
        and job["name"] not in SYSTEM_JOB_NAMES
        and not job["name"].startswith(_SYSTEM_GENERATED_PREFIXES)
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
        len(user_jobs_to_restore),
        str(tenant.id)[:8],
        snapshot_at,
    )

    restored = 0
    errors = 0
    for job in user_jobs_to_restore:
        # Strip gateway-internal fields that cron.add rejects
        _STRIP_FIELDS = {
            "id",
            "jobId",
            "createdAt",
            "state",
            "createdAtMs",
            "updatedAtMs",
            "nextRunAtMs",
            "runningAtMs",
        }
        clean_job = {k: v for k, v in job.items() if k not in _STRIP_FIELDS}
        try:
            invoke_gateway_tool(tenant, "cron.add", {"job": clean_job})
            restored += 1
        except GatewayError as exc:
            logger.warning(
                "Failed to restore cron job '%s' for tenant %s: %s",
                job.get("name"),
                str(tenant.id)[:8],
                exc,
            )
            errors += 1

    return {"restored": restored, "errors": errors}


def seed_cron_jobs(tenant: Tenant | str) -> dict:
    """Seed default cron jobs for a tenant.

    Postgres-canonical path (``tenant.postgres_cron_canonical=True``):
    upsert ``CronJob`` rows from ``build_cron_seed_jobs`` and mark them
    ``source='system'``, ``managed=True``. The reconciler picks up the
    delta and pushes to the container's SQLite via signal.

    Legacy path: directly call ``cron.add`` against the gateway, diffing
    by name.
    """
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

    # Postgres-canonical path: write CronJob rows; signal triggers reconcile.
    if getattr(tenant, "postgres_cron_canonical", False):
        from apps.cron.models import CronJob, CronJobSource

        created = 0
        existing_names = set(
            CronJob.objects.filter(tenant=tenant, source=CronJobSource.SYSTEM).values_list("name", flat=True)
        )
        for job in jobs:
            name = job.get("name", "")
            if not name or name.lower() in {n.lower() for n in existing_names}:
                continue
            CronJob.objects.create(
                tenant=tenant,
                name=name,
                data=job,
                source=CronJobSource.SYSTEM,
                managed=True,
                enabled=bool(job.get("enabled", True)),
            )
            created += 1
        logger.info(
            "seed_cron_jobs (postgres-canonical): tenant %s — created %d system rows (existing=%d, total=%d)",
            tenant_id,
            created,
            len(existing_names),
            len(jobs),
        )
        return {
            "tenant_id": tenant_id,
            "jobs_total": len(jobs),
            "created": created,
            "errors": 0,
            "via": "postgres_canonical",
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
    existing_names = {j.get("name", "").lower() for j in existing_jobs if isinstance(j, dict) and j.get("name")}
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
                user_restore["restored"],
                tenant_id,
            )
    except Exception:
        logger.warning(
            "seed_cron_jobs: user job restore failed for tenant %s (non-fatal)",
            tenant_id,
            exc_info=True,
        )

    # Safety-net dedup after restore — catch any duplicates introduced by restore
    if user_restore.get("restored", 0) > 0:
        try:
            dedup_tenant_cron_jobs(tenant)
        except Exception:
            logger.warning(
                "seed_cron_jobs: post-restore dedup failed for tenant %s (non-fatal)",
                tenant_id,
                exc_info=True,
            )

    return {
        "tenant_id": tenant_id,
        "jobs_total": len(jobs),
        "created": created,
        "errors": errors,
        "skipped_existing": len(existing_names),
        "user_jobs_restored": user_restore["restored"],
    }


def refresh_system_cron_rows_from_seed(tenant: Tenant | str) -> dict:
    """Sync the Postgres CronJob rows for system crons to ``build_cron_seed_jobs``.

    Postgres is canonical for postgres-canonical tenants. This function is
    the seed-side writer: it ensures every system cron in ``build_cron_seed_jobs``
    has a matching CronJob row whose ``data`` is current. The signal handler
    (``apps/cron/signals.py``) fires ``regenerate_tenant_crons`` to push any
    drift to the container's gateway.

    User customizations are preserved when the row's stored message body
    matches none of the ``_KNOWN_DEFAULT_PREFIXES`` (i.e. the user pasted
    a fresh prompt via the dashboard). Non-message fields (model, kind,
    schedule, delivery, sessionTarget) are always refreshed from seed —
    those are not user-customizable surfaces.

    Called from ``update_tenant_config``, the management command, and the
    post-image-swap restore (so a fresh container converges).
    """
    if isinstance(tenant, str):
        tenant = Tenant.objects.select_related("user").get(id=tenant)

    from apps.cron.models import CronJob, CronJobSource

    from .cron_drift import strip_date_line

    seed_jobs = build_cron_seed_jobs(tenant)
    summary = {"created": 0, "updated": 0, "preserved_custom": 0, "unchanged": 0, "reaped": 0}

    for job in seed_jobs:
        name = job.get("name", "")
        if not name:
            continue

        row = CronJob.objects.filter(tenant=tenant, name=name).first()
        if row is None:
            CronJob.objects.create(
                tenant=tenant,
                name=name,
                data=job,
                source=CronJobSource.SYSTEM,
                managed=True,
                enabled=bool(job.get("enabled", True)),
            )
            summary["created"] += 1
            continue

        existing_payload = row.data.get("payload") if isinstance(row.data, dict) else None
        existing_message = ""
        if isinstance(existing_payload, dict):
            existing_message = existing_payload.get("message") or ""
        existing_body = strip_date_line(existing_message).strip()
        is_default = (not existing_body) or any(existing_body.startswith(prefix) for prefix in _KNOWN_DEFAULT_PREFIXES)

        if is_default:
            # Safe to overwrite — message body matches a known default
            # template (or is empty). Always pull non-message fields
            # from seed regardless.
            new_data = dict(job)
        else:
            # User pasted a custom message — preserve it. Refresh
            # everything else (model, kind, schedule, delivery, sessionTarget).
            new_data = dict(job)
            seed_payload = new_data.get("payload") or {}
            if isinstance(seed_payload, dict) and isinstance(existing_payload, dict):
                merged_payload = dict(seed_payload)
                # Custom message wins; non-message payload fields come from seed.
                if "message" in existing_payload:
                    merged_payload["message"] = existing_payload["message"]
                new_data["payload"] = merged_payload
            summary["preserved_custom"] += 1

        if row.data == new_data:
            summary["unchanged"] += 1
            continue

        row.data = new_data
        row.save(update_fields=["data", "updated_at"])
        summary["updated"] += 1

    # Reap managed system crons that have fallen out of the seed — e.g.
    # "Heartbeat Check-in" once the tenant moves to the built-in heartbeat, or
    # "Gravity Weekly Check-in" while Gravity is paused. Without this they
    # linger as orphaned rows that keep firing on a stale model. The CronJob
    # post_delete signal pushes the removal to the container. Platform-managed
    # ``_sync:``/``_fuel:`` crons have their own lifecycle — leave them.
    seed_names = {job.get("name", "") for job in seed_jobs}
    for orphan in list(
        CronJob.objects.filter(tenant=tenant, source=CronJobSource.SYSTEM, managed=True).exclude(name__in=seed_names)
    ):
        if orphan.name.startswith(_SYSTEM_GENERATED_PREFIXES):
            continue
        orphan.delete()
        summary["reaped"] += 1

    logger.info(
        "refresh_system_cron_rows_from_seed: tenant %s — created=%d updated=%d "
        "preserved_custom=%d unchanged=%d reaped=%d",
        str(tenant.id)[:8],
        summary["created"],
        summary["updated"],
        summary["preserved_custom"],
        summary["unchanged"],
        summary["reaped"],
    )
    return summary


# Known default prompt prefixes, post-date-strip. If a stored cron message
# body (after stripping the leading ``Current date and time:`` preamble)
# starts with one of these, we treat it as platform-managed and safe to
# overwrite from the current seed. Anything else is treated as a user
# customization and preserved on refresh.
#
# Add a prefix here when a seed prompt's opening sentence changes — the
# old form must remain in the list for one release so existing rows still
# match as "default" and get rolled forward. Old entries can be pruned
# once the fleet has converged.
_KNOWN_DEFAULT_PREFIXES = (
    # Shared preamble injected by ``_prepare_cron_prompt`` before every
    # prompt-template body. This is what stored cron messages actually
    # start with after ``strip_date_line``, since the preamble comes AFTER
    # the date line. Without this entry the entire fleet was being
    # classified as user-customized and seed refreshes were skipping the
    # message body (canary 2026-05-14 02:48 sweep logged
    # ``preserved_custom=10`` for the canary alone).
    "**MANDATORY — do this BEFORE",
    # Per-cron prompt bodies. Only relevant if a future refactor moves
    # the shared preamble somewhere else.
    "Good morning! Create today's morning briefing",
    "It's evening check-in time.",
    "It's Monday morning. Run the Week Ahead Review",
    "Background maintenance run.",
    "You received a scheduled check-in.",
    "Sunday-evening Gravity check-in.",
    "Personal-question cron. Pick ONE thoughtful",
    # Defensive: the date line itself, for any caller that passes the
    # raw stored message without ``strip_date_line``.
    "Current date and time:",
)


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
    except GatewayError as exc:
        # Container-unavailable signals a wake/race the deferral helper handles —
        # let it propagate. Other gateway flakes are non-fatal here.
        from apps.cron.cache import is_container_unavailable_error

        if is_container_unavailable_error(exc):
            raise
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
        "Sunday-evening Gravity check-in.",
        "Personal-question cron. Pick ONE thoughtful",
        # Date-injected variants (added 2026-03-08):
        "Current date and time:",
    ]

    # System job names that may need delete+recreate when payload changes
    # (because OpenClaw rejects payload patches via cron.update). The
    # universal isolation refactor (2026-04) reshapes payloads from
    # systemEvent/text → agentTurn/message + Phase 2 sync block, so this
    # path is the migration channel for existing tenants.
    _SYSTEM_JOB_NAMES = {
        "Morning Briefing",
        "Evening Check-in",
        "Personal Question",
        "Weekly Reflection",
        "Week Ahead Review",
        "Background Tasks",
        "Project Check-in",
        "Heartbeat Check-in",
        "Gravity Weekly Check-in",
    }

    def _is_default_prompt(existing_message: str) -> bool:
        """Return True if the existing prompt matches a known default (old or current)."""
        msg = existing_message.strip()
        return any(msg.startswith(prefix) for prefix in _KNOWN_DEFAULT_PREFIXES)

    def _strip_date_line(message: str) -> str:
        """Strip the leading 'Current date and time:' preamble line.

        ``_prepare_cron_prompt`` injects today's date at the top of every
        cron message, which means existing-vs-desired comparison would
        otherwise differ every day and trigger churn. Compare the
        structural body only.

        Stability of this comparison also depends on
        ``_build_cron_message`` returning a string that matches what
        OpenClaw stores back via ``cron.list``. OC strips trailing
        whitespace on store (``coercePayload`` → ``normalizeOptionalString``
        → ``value?.trim()``), so ``_build_cron_message`` calls ``.strip()``
        on its output to mirror that. If a future OpenClaw bump adds more
        normalization (e.g., line-ending conversion, NFC unicode),
        ``project_openclaw_cron_payload_shape.md`` lists the audit step.
        """
        if not isinstance(message, str):
            return ""
        if not message.startswith("Current date and time:"):
            return message
        # The date preamble ends at the first blank line (\n\n)
        idx = message.find("\n\n")
        if idx == -1:
            return message
        return message[idx + 2 :]

    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")

    updated = 0
    skipped = 0
    errors = 0

    # Fields the user can't customize, so any drift must trigger a
    # recreate. `model` is the canary-2026-05-12 case: a stale
    # anthropic-cli/... value left over from BYO setup that Django no
    # longer sets but OpenClaw still has stored. `kind` covers the
    # legacy systemEvent → agentTurn migration. New fields with the
    # same property go here.
    _NON_MESSAGE_PAYLOAD_DRIFT_FIELDS = ("model", "kind")

    def _payload_non_message_drift(existing: dict, desired: dict) -> list[str]:
        """Return non-message payload fields that drifted between sides.

        Compares full job defs so we can fall back to the top-level
        `model` field on the desired side. OpenClaw normalizes top-level
        `model` into `payload.model` on cron.add (observed on Heartbeat
        2026-05-12 16:11 sweep), so:

            desired.model = "openrouter/minimax/..."   (top-level)
            desired.payload = {kind, message}          (no model)
            existing.payload.model = "openrouter/minimax/..."  (stored)

        A naive `existing.payload.model != desired.payload.model` says
        DRIFT every time and triggers a no-op recreate on every sweep
        — same churn class as PR #505. The fix is to OR in the
        top-level field on the desired side. The existing side is read
        from payload only, since OpenClaw always stores it there.
        """
        existing_payload = existing.get("payload", {}) if isinstance(existing, dict) else {}
        desired_payload = desired.get("payload", {}) if isinstance(desired, dict) else {}
        if not isinstance(existing_payload, dict) or not isinstance(desired_payload, dict):
            return []
        drift = []
        for field in _NON_MESSAGE_PAYLOAD_DRIFT_FIELDS:
            existing_value = existing_payload.get(field)
            desired_value = desired_payload.get(field)
            if desired_value is None and field == "model":
                # Top-level fallback for cron defs that pin model at the
                # outer level (Heartbeat). Other fields are payload-only
                # by convention so no fallback needed.
                desired_value = desired.get(field) if isinstance(desired, dict) else None
            if existing_value != desired_value:
                drift.append(field)
        return drift

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

        # Non-message payload drift (model, kind). These fields the user
        # can't customize — if they differ, the cron is either misrouted
        # (stale `model` rejected by the agents allowlist → preflight
        # failure, see canary 2026-05-12) or shape-broken (legacy
        # systemEvent vs current agentTurn). Force recreate regardless
        # of message customization: a custom prompt the user can re-set
        # is recoverable; a silently-failing cron isn't.
        non_message_drift = _payload_non_message_drift(existing, desired)

        # Compare structural body only — strip the date preamble that
        # _prepare_cron_prompt injects, otherwise every refresh would
        # churn even when nothing semantic changed.
        message_differs = _strip_date_line(existing_message) != _strip_date_line(desired_message)
        if message_differs:
            if _is_default_prompt(existing_message) or non_message_drift:
                patch["payload"] = desired_payload
                if non_message_drift and not _is_default_prompt(existing_message):
                    logger.warning(
                        "update_system_cron_prompts: recreating '%s' for tenant %s "
                        "due to non-message payload drift (%s); user-customized "
                        "message will be reset to default",
                        name,
                        tenant_id,
                        ",".join(non_message_drift),
                    )
            else:
                logger.info(
                    "update_system_cron_prompts: skipping '%s' for tenant %s (user-customized)",
                    name,
                    tenant_id,
                )
                skipped += 1
        elif non_message_drift:
            # Message matches but model/kind drifted — recreate to repair.
            patch["payload"] = desired_payload
            logger.info(
                "update_system_cron_prompts: recreating '%s' for tenant %s due to non-message payload drift (%s)",
                name,
                tenant_id,
                ",".join(non_message_drift),
            )

        # Check timezone: always fix if wrong
        existing_schedule = existing.get("schedule", {})
        existing_tz = existing_schedule.get("tz", "UTC")
        if existing_tz != user_tz:
            patch["schedule"] = desired.get("schedule", {})
            logger.info(
                "update_system_cron_prompts: fixing tz '%s' -> '%s' for '%s' tenant %s",
                existing_tz,
                user_tz,
                name,
                tenant_id,
            )

        if not patch:
            continue  # Nothing to update

        # Historical context: pre-5.x OpenClaw rejected payload changes via
        # cron.update. 2026.5.7's `mergeCronPayload` (jobs-DIMVdW2S.js:715)
        # actually accepts payload patches now, including kind transitions —
        # but we keep the delete+create path here because it's the proven
        # migration channel for legacy systemEvent → agentTurn shape changes
        # and the in-flight churn fix is one source of risk at a time. If
        # you flip this to cron.update + payload patch, also drop the
        # delete+create branch and the now-defensive `kind` check, and add
        # a test that asserts a single mutation per job (vs the current 2).
        if "payload" in patch and name in _SYSTEM_JOB_NAMES:
            try:
                gateway_job_id = existing.get("id") or existing.get("jobId") or job_id
                invoke_gateway_tool(
                    tenant,
                    "cron.remove",
                    {"jobId": gateway_job_id},
                )
                invoke_gateway_tool(tenant, "cron.add", {"job": desired})
                updated += 1
                logger.info(
                    "update_system_cron_prompts: recreated '%s' for tenant %s (payload changed — used delete+create)",
                    name,
                    tenant_id,
                )
                continue
            except GatewayError as exc:
                from apps.cron.cache import is_container_unavailable_error

                if is_container_unavailable_error(exc):
                    raise
                logger.exception(
                    "update_system_cron_prompts: delete+create failed for '%s' tenant %s",
                    name,
                    tenant_id,
                )
                errors += 1
                continue

        try:
            invoke_gateway_tool(tenant, "cron.update", {"jobId": job_id, "patch": patch})
            updated += 1
            logger.info("update_system_cron_prompts: updated '%s' for tenant %s", name, tenant_id)
        except GatewayError as exc:
            from apps.cron.cache import is_container_unavailable_error

            if is_container_unavailable_error(exc):
                raise
            logger.exception("update_system_cron_prompts: failed to update '%s' for %s", name, tenant_id)
            errors += 1

    # --- Seed missing system jobs ---
    # The patch loop above only updates EXISTING jobs (line ~1179: continues
    # when `name not in existing_by_name`). New system jobs added to the
    # default set won't reach existing tenants without an explicit add pass.
    for desired in desired_jobs:
        name = desired.get("name", "")
        if name in existing_by_name:
            continue
        if name not in _SYSTEM_JOB_NAMES:
            continue
        try:
            invoke_gateway_tool(tenant, "cron.add", {"job": desired})
            updated += 1
            logger.info(
                "update_system_cron_prompts: seeded missing '%s' for tenant %s",
                name,
                tenant_id,
            )
        except GatewayError as exc:
            from apps.cron.cache import is_container_unavailable_error

            if is_container_unavailable_error(exc):
                raise
            logger.exception(
                "update_system_cron_prompts: failed to seed '%s' for tenant %s",
                name,
                tenant_id,
            )
            errors += 1

    # --- Heartbeat add/remove drift correction ---
    sync_heartbeat_cron(tenant, existing_by_name)

    # Push fresh USER.md alongside cron-prompt refreshes. force=True so the
    # management command and HTTP refresh paths always emit a current
    # envelope, not gated by the post-save signal debounce window.
    try:
        from .workspace_envelope import push_user_md

        push_user_md(tenant, force=True)
    except Exception:
        logger.warning(
            "update_system_cron_prompts: USER.md refresh failed for tenant %s (non-fatal)",
            tenant_id,
            exc_info=True,
        )

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
            list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        except GatewayError as exc:
            # Container-unavailable propagates so the deferral helper can see it;
            # other flakes degrade to "error" status as before.
            from apps.cron.cache import is_container_unavailable_error

            if is_container_unavailable_error(exc):
                raise
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
                    tenant,
                    "cron.update",
                    {"jobId": job_id, "patch": {"schedule": desired_hb["schedule"]}},
                )
                logger.info(
                    "sync_heartbeat_cron: updated schedule for tenant %s (%s → %s)",
                    tenant.id,
                    existing_expr,
                    desired_expr,
                )
                return "updated"

    except GatewayError as exc:
        from apps.cron.cache import is_container_unavailable_error

        if is_container_unavailable_error(exc):
            raise
        logger.exception("sync_heartbeat_cron: failed for tenant %s", tenant.id)
        return "error"

    return "ok"


def check_tenant_health(tenant_id: str) -> dict:
    """Check if a tenant's OpenClaw instance is healthy.

    Pings the container's gateway health endpoint and returns structured
    results including response time and config version drift.
    """
    import httpx

    tenant = Tenant.objects.select_related("user").get(id=tenant_id)
    result = {
        "tenant_id": str(tenant.id),
        "display_name": tenant.user.display_name,
        "status": tenant.status,
        "container": tenant.container_id,
        "healthy": False,
        "checks": {},
    }

    if tenant.status != Tenant.Status.ACTIVE:
        result["checks"]["status"] = {"ok": False, "detail": f"tenant is {tenant.status}"}
        return result

    if not tenant.container_fqdn:
        result["checks"]["container"] = {"ok": False, "detail": "no container FQDN"}
        return result

    # Config version drift — informational only, does NOT affect healthy status.
    # Drift is expected after deploys and resolves on the next idle cycle.
    pending = tenant.pending_config_version or 0
    current = tenant.config_version or 0
    config_drift = pending > current
    result["config_drift"] = config_drift
    if config_drift:
        result["config_drift_detail"] = f"current={current} pending={pending}"

    # Ping gateway health endpoint — this IS a health signal
    health_url = f"https://{tenant.container_fqdn}/health"
    try:
        resp = httpx.get(health_url, timeout=10)
        response_time_ms = int(resp.elapsed.total_seconds() * 1000)
        is_healthy = resp.status_code == 200
        result["checks"]["gateway"] = {
            "ok": is_healthy,
            "status_code": resp.status_code,
            "response_time_ms": response_time_ms,
        }
    except httpx.TimeoutException:
        result["checks"]["gateway"] = {"ok": False, "detail": "timeout (10s)"}
    except httpx.ConnectError:
        result["checks"]["gateway"] = {"ok": False, "detail": "connection refused"}
    except Exception as exc:
        result["checks"]["gateway"] = {"ok": False, "detail": str(exc)[:200]}

    result["healthy"] = all(c["ok"] for c in result["checks"].values())
    return result


def check_all_tenants_health() -> list[dict]:
    """Run health checks on all active tenants. Returns list of results."""
    results = []
    for tenant in Tenant.objects.filter(status=Tenant.Status.ACTIVE).select_related("user"):
        try:
            result = check_tenant_health(str(tenant.id))
            results.append(result)
        except Exception as exc:
            results.append(
                {
                    "tenant_id": str(tenant.id),
                    "display_name": tenant.user.display_name,
                    "healthy": False,
                    "error": str(exc)[:200],
                }
            )
    return results
