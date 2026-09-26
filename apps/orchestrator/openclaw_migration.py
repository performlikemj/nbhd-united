"""Explicit, image-first OpenClaw 9.4 migration with durable resume checkpoints.

Network operations always run outside DB transactions. The record is private:
its source cron export contains reminder payloads and must never be printed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import time
import uuid
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.cron.models import CronJob
from apps.cron.share_cron_sync import _desired_jobs
from apps.cron.signals import suppress_cronjob_reconcile
from apps.tenants.models import Tenant

from . import azure_client, runtime_operator

VERSION = "2026.9.4"
STEPS = ("preflight", "capture", "image", "version", "config", "crons", "verify")
RECOVERY = (
    "Stopped; no automatic rollback. Keep the tenant and migration record intact. "
    "Resolve the failed step, then rerun the same tenant/tag to resume. "
    "See docs/runbooks/openclaw-94-migration.md for controlled manual recovery. "
    "Never use an image-only 9.4 -> 5.28 rollback."
)


class MigrationError(RuntimeError):
    pass


class VerificationError(MigrationError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _owned_query(tenant, record):
    query = Tenant.objects.filter(pk=tenant.pk)
    owner = record.get("owner_token")
    if owner:
        return query.filter(openclaw_migration__owner_token=owner)
    # Legacy/direct helper calls must never overwrite a claimed migration.
    return query.exclude(openclaw_migration__has_key="owner_token")


def _assert_owner(tenant, record):
    if not _owned_query(tenant, record).exists():
        raise MigrationError("migration_owner_fenced")


def _save(tenant, record):
    record["updated_at"] = timezone.now().isoformat()
    record["lease_until"] = (timezone.now() + timedelta(minutes=10)).isoformat()
    if record.get("owner_token"):
        from .hibernation import _update_tenant_after_azure

        # Keep the existing stale-connection retry, with the fence on BOTH attempts.
        updated = _update_tenant_after_azure(
            tenant.pk, filters={"openclaw_migration__owner_token": record["owner_token"]}, openclaw_migration=record
        )
    else:
        updated = _owned_query(tenant, record).update(openclaw_migration=record)
    if updated != 1:
        raise MigrationError("migration_owner_fenced")
    tenant.openclaw_migration = copy.deepcopy(record)


def _check_lease(record, takeover, confirm_owner_dead=False):
    if bool(takeover) != bool(confirm_owner_dead):
        raise MigrationError("Takeover requires --takeover <exact owner token> --confirm-owner-dead")
    if takeover:
        if record.get("owner_token") != takeover or record.get("status") != "RUNNING":
            raise MigrationError("dead_owner_token_mismatch")
        return  # Explicit operator attestation of termination; compare again under row lock.
    if record.get("status") == "RUNNING":
        raise MigrationError("Migration already running; use --takeover <exact owner token> --confirm-owner-dead")


def registry_digest(image: str) -> str:
    """Resolve through ACR's data plane using the system-assigned identity.

    No CLI dependency, user-assigned client ID, token logging or redirects.
    """
    from azure.identity import ManagedIdentityCredential

    try:
        registry, reference = image.split("/", 1)
        if not re.fullmatch(r"[a-z0-9]+\.azurecr\.io", registry):
            raise ValueError("registry")
        if "@" in reference:
            repository, ref = reference.split("@", 1)
            repository = repository.split(":", 1)[0]
        else:
            repository, ref = reference.rsplit(":", 1)
        if repository != "nbhd-openclaw" or not re.fullmatch(r"[A-Za-z0-9_.:-]+", ref):
            raise ValueError("reference")
        credential = ManagedIdentityCredential()
        try:
            aad = credential.get_token("https://containerregistry.azure.net/.default").token
        finally:
            credential.close()
        exchange = requests.post(
            f"https://{registry}/oauth2/exchange",
            data={"grant_type": "access_token", "service": registry, "access_token": aad},
            timeout=30,
            allow_redirects=False,
        )
        exchange.raise_for_status()
        token = requests.post(
            f"https://{registry}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "service": registry,
                "scope": f"repository:{repository}:pull",
                "refresh_token": exchange.json()["refresh_token"],
            },
            timeout=30,
            allow_redirects=False,
        )
        token.raise_for_status()
        response = requests.head(
            f"https://{registry}/v2/{repository}/manifests/{ref}",
            headers={
                "Authorization": "Bearer " + token.json()["access_token"],
                "Accept": ", ".join(
                    (
                        "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.oci.image.index.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                    )
                ),
            },
            timeout=30,
            allow_redirects=False,
        )
        response.raise_for_status()
        digest = response.headers.get("Docker-Content-Digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("digest")
        if "@" in reference and digest != ref:
            raise ValueError("digest mismatch")
        return digest
    except Exception:
        raise MigrationError("acr_digest_unavailable") from None


def get_app(tenant):
    return azure_client.get_container_client().container_apps.get(settings.AZURE_RESOURCE_GROUP, tenant.container_id)


def _image(app):
    return next(c.image for c in app.template.containers if c.name == "openclaw")


def _snapshot_template(app):
    """Preserve structure and secret refs; literal env values can contain secrets."""
    template = copy.deepcopy(app.template.as_dict())
    for container in template.get("containers", []):
        for env in container.get("env", []):
            if "value" in env and env.get("name") not in azure_client._OC_STATE_ENV:
                env["value"] = "[REDACTED: restore from authoritative config/Key Vault]"
    return template


def preflight(tenant, record):
    if azure_client.is_mock():
        raise MigrationError("Real migration refuses AZURE_MOCK=true")
    app = get_app(tenant)
    target = f"{settings.AZURE_ACR_SERVER}/nbhd-openclaw:{record['tag']}"
    digest = registry_digest(target)
    source_image = _image(app)
    source_digest = registry_digest(source_image)
    if VERSION in source_image and tenant.openclaw_version != VERSION:
        raise MigrationError("Unrecorded image-only partial upgrade; old HTTP cron truth is unavailable")
    return {
        "target_image": target + "@" + digest,
        "target_digest": digest,
        "revision_suffix": "m94-" + uuid.uuid4().hex[:12],
        "source": {
            "image": source_image,
            "digest": source_digest,
            "tag": tenant.container_image_tag,
            "version": tenant.openclaw_version,
            "hibernated_at": None,
            "template": _snapshot_template(app),
            "postgres_cron_canonical": tenant.postgres_cron_canonical,
        },
    }


class PreservationError(MigrationError):
    def __init__(self, reasons):
        self.reasons = reasons
        super().__init__("BLOCKED_UNSUPPORTED " + ",".join(f"{k}={v}" for k, v in sorted(reasons.items())))


def canonical_inventory(tenant):
    """All canonical declarations, BEFORE enabled/time/ownership filtering."""
    from .cron_reconcile import _row_to_cron_dict

    declarations = []
    for row in CronJob.objects.filter(tenant=tenant).order_by("id"):
        job = _row_to_cron_dict(row)
        job["declarationKey"] = f"nbhd:{row.pk}"
        job["id"] = f"nbhd:{row.pk}"
        declarations.append((job, row.managed))
    return declarations


def projected_canonical(canonical):
    """Canonical rows the signed writer can project.

    Disabled rows are never projected (the signer skips them) and stay in
    Postgres unchanged, so enabling one later goes through the ordinary 9.4
    path. Nothing about them is lost by the image swap.
    """
    return [(job, managed) for job, managed in canonical if job.get("enabled", True) and not _fuel_owned(job)]


def _fuel_owned(job):
    """``_fuel:*`` rows/jobs mirror the Fuel models, which own the desired set.

    The signed file carries that set (share_cron_sync._fuel_jobs), so neither
    a stale Postgres mirror row nor a 5.28 runtime copy is preserved as-is.
    """
    return str(job.get("name") or "").startswith("_fuel:")


def preservation_precheck(tenant, jobs, *, record=None):
    from .migration_preservation import reason_counts

    canonical = canonical_inventory(tenant) if tenant.postgres_cron_canonical else []
    # Imminent obligations take priority and cannot disappear through filtering.
    assert_cutover_safe(jobs + [j for j, _ in canonical])
    versions = (record if record is not None else tenant.openclaw_migration or {}).get("imported_versions", {})
    # Canonical rows win conflicts (capture imports with preserve_existing), so
    # a live copy of a canonical name is never written, EXCEPT rows this
    # migration imported and nobody edited since: capture re-captures those
    # from live. Every live job that capture can write needs proof.
    recaptured = {
        row.name
        for row in CronJob.objects.filter(tenant=tenant, name__in=list(versions))
        if versions.get(row.name) == row.updated_at.isoformat()
    }
    protected = {j["name"] for j, _ in canonical} - recaptured
    written = [j for j in jobs if j["name"] not in protected and not _fuel_owned(j)]
    reasons = reason_counts([(j, True) for j in written] + projected_canonical(canonical))
    names = {j["name"] for j in jobs}
    missing_owned = sum(
        1
        for row in CronJob.objects.filter(tenant=tenant, enabled=True)
        if versions.get(row.name) == row.updated_at.isoformat() and row.name not in names
    )
    if missing_owned:
        reasons["source_cancellation_not_projected"] = missing_owned
    if reasons:
        raise PreservationError(reasons)
    return canonical


def _live_jobs(tenant):
    from apps.cron.gateway_client import invoke_gateway_tool

    from .cron_reconcile import _complete_cron_observation

    response = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True}, metadata_only=True)
    jobs = _complete_cron_observation(response)
    if jobs is None or not isinstance(jobs, list):
        raise MigrationError("source_observation_incomplete")
    if any(not isinstance(j, dict) or not j.get("name") or not (j.get("id") or j.get("jobId")) for j in jobs):
        raise MigrationError("source_identity_missing")
    return jobs


def live_source_jobs(tenant, *, spent=None):
    """Complete live export without spent sync notices (collected into ``spent``)."""
    jobs = _live_jobs(tenant)
    notices = _spent_sync_notices(jobs)
    if spent is not None:
        spent.extend(notices)
    jobs = [job for job in jobs if not any(job is notice for notice in notices)]
    if len({j["name"] for j in jobs}) != len(jobs):
        raise MigrationError("source_names_duplicated")
    return jobs


# Phase 2 sync notices (apps/integrations/runtime_views.py) are date-pinned
# ``_sync:<job>`` systemEvent crons created two minutes before they fire.
# Once past that window they are spent leftovers, never imported or written;
# a fresh one is an imminent obligation, so the tenant waits. Only the exact
# notice form qualifies: any other ``_sync:`` job goes through the precheck.
_SYNC_NOTICE_SPENT_MS = 60 * 60 * 1000
_SYNC_NOTICE_EXPR = re.compile(r"\d{1,2} \d{1,2} \d{1,2} \d{1,2} \*")


def _is_sync_notice(job):
    schedule, payload = job.get("schedule") or {}, job.get("payload") or {}
    return (
        str(job["name"]).startswith("_sync:")
        and schedule.get("kind") == "cron"
        and isinstance(schedule.get("expr"), str)
        and bool(_SYNC_NOTICE_EXPR.fullmatch(schedule["expr"]))
        and payload.get("kind") == "systemEvent"
        and str(payload.get("text", "")).startswith("[Sync — ")
        and job.get("sessionTarget") == "main"
    )


def _spent_sync_notices(jobs):
    now = int(timezone.now().timestamp() * 1000)
    spent = []
    for job in filter(_is_sync_notice, jobs):
        created = job.get("createdAtMs")
        if type(created) is not int or created > now - _SYNC_NOTICE_SPENT_MS:
            raise MigrationError("cron_imminent")
        spent.append(job)
    return spent


def remove_spent_sync_notices(tenant, spent):
    """Delete spent notices on the SOURCE runtime, after the precheck passed.

    The runtime cron store survives the image swap, where an unmatched legacy
    row fails verification. Date-pinned 5-field expressions also re-fire a
    year later with stale text, so they are removed rather than carried.
    A re-list proves the removal stuck.
    """
    from apps.cron.gateway_client import invoke_gateway_tool

    for job in spent:
        invoke_gateway_tool(tenant, "cron.remove", {"jobId": job.get("id") or job.get("jobId")}, metadata_only=True)
    remaining = {j.get("id") or j.get("jobId") for j in _live_jobs(tenant)}
    if remaining & {j.get("id") or j.get("jobId") for j in spent}:
        raise MigrationError("sync_notice_cleanup_failed")


def report_tenant(tenant):
    """Inventory only: no checkpoint, canonical flip, import or Azure write."""
    record = tenant.openclaw_migration or {}
    if record.get("status") == "RUNNING":
        lease = parse_datetime(record.get("lease_until") or "")
        updated = parse_datetime(record.get("updated_at") or "")
        return {
            "status": "RUNNING",
            "reasons": {},
            "owner_token": record.get("owner_token", "missing"),
            "lease_age_seconds": int((timezone.now() - updated).total_seconds()) if updated else "unknown",
            "lease_expired": lease is None or lease <= timezone.now(),
        }
    if tenant.openclaw_version == VERSION:
        return {"status": "ALREADY_94", "reasons": {}}
    if (
        tenant.hibernated_at
        or tenant.status != Tenant.Status.ACTIVE
        or not tenant.container_id
        or not tenant.container_fqdn
    ):
        return {"status": "DEFER", "reasons": {"tenant_unavailable": 1}}
    try:
        if not tenant.postgres_cron_canonical and CronJob.objects.filter(tenant=tenant, creation_path="typed").exists():
            raise MigrationError("canonical_ownership_uncertain")
        jobs = live_source_jobs(tenant)
        preservation_precheck(tenant, jobs)
        return {
            "status": "READY",
            "reasons": {},
            "quarantined_cache": (
                CronJob.objects.filter(tenant=tenant).exclude(name__in=[j["name"] for j in jobs]).count()
                if not tenant.postgres_cron_canonical
                else 0
            ),
        }
    except PreservationError as exc:
        return {"status": "BLOCKED_UNSUPPORTED", "reasons": exc.reasons}
    except Exception as exc:
        reason = (
            str(exc) if isinstance(exc, MigrationError) and re.fullmatch(r"[a-z_]+", str(exc)) else "source_unavailable"
        )
        return {"status": "DEFER", "reasons": {reason: 1}}


def capture(tenant, record):
    from apps.cron.postgres_canonical import upsert_from_gateway_jobs

    spent = []
    jobs = live_source_jobs(tenant, spent=spent)
    canonical = preservation_precheck(tenant, jobs, record=record)
    if spent:
        remove_spent_sync_notices(tenant, spent)
    # Keep obligations from all captures; current time filtering is never an audit.
    history = record.setdefault("canonical_one_shots", {})
    for declaration, _ in canonical:
        if (declaration.get("schedule") or {}).get("kind") == "at":
            from .migration_preservation import declaration_digest

            history[declaration["id"] + ":" + declaration_digest(declaration)] = declaration
    previous = record.get("cron_export")
    if previous is not None and previous != jobs:
        record.setdefault("cron_export_history", []).append({"at": record.get("cron_export_at"), "jobs": previous})
        fresh_ids = {j.get("id") or j.get("jobId") for j in jobs}
        fresh_names = {j["name"] for j in jobs}
        now_ms = int(timezone.now().timestamp() * 1000)
        from .migration_preservation import authored_at_ms as _at_fires_at_ms

        for old in previous:
            old_id = old.get("id") or old.get("jobId")
            # Absence BEFORE its due time proves cancellation/replacement; an
            # expired missing job still requires delivery evidence.
            due = _at_fires_at_ms(old) if (old.get("schedule") or {}).get("kind") == "at" else None
            if old_id not in fresh_ids and due and due > now_ms:
                record.setdefault("source_resolutions", {})[old_id] = {
                    "disposition": "superseded_at_source" if old["name"] in fresh_names else "cancelled_at_source",
                    "observed_at": timezone.now().isoformat(),
                    "source": "complete_live_recapture_before_due",
                }
    # Record import ownership BEFORE the first DB write, so interrupted imports
    # can update their own rows without replacing pre-existing canonical truth.
    owned = set(record.get("imported_names", []))
    if "imported_names" not in record:
        existing = set(CronJob.objects.filter(tenant=tenant).values_list("name", flat=True))
        owned = {j["name"] for j in jobs} - existing if tenant.postgres_cron_canonical else {j["name"] for j in jobs}
    else:
        existing = set(CronJob.objects.filter(tenant=tenant).values_list("name", flat=True))
        owned |= {j["name"] for j in jobs} - existing
    record["imported_names"] = sorted(owned)
    record["cron_export"] = jobs
    record["cron_export_at"] = timezone.now().isoformat()
    record["cron_source"] = "live HTTP cron.list, includeDisabled=true; no fallback"
    _save(tenant, record)
    # Canonical rows win conflicts; the export remains available for recovery.
    # A noncanonical import cannot replace typed rows from a different authority.
    if not tenant.postgres_cron_canonical and CronJob.objects.filter(tenant=tenant, creation_path="typed").exists():
        raise MigrationError("Cron provenance uncertain: noncanonical tenant has typed Postgres rows")
    with suppress_cronjob_reconcile(), transaction.atomic():
        existing_rows = {r.name: r for r in CronJob.objects.select_for_update().filter(tenant=tenant)}
        quarantined = list(record.get("quarantined_cache", []))
        if not tenant.postgres_cron_canonical:
            names = {j["name"] for j in jobs}
            for name, row in list(existing_rows.items()):
                if name not in names:
                    quarantined.append(
                        {
                            "id": row.pk,
                            "name": row.name,
                            "data": row.data,
                            "enabled": row.enabled,
                            "managed": row.managed,
                            "source": row.source,
                            "gateway_job_id": row.gateway_job_id,
                            "creation_path": row.creation_path,
                            "pattern": row.pattern,
                            "typed_payload": row.typed_payload,
                            **{
                                field: getattr(row, field).isoformat() if getattr(row, field) else None
                                for field in (
                                    "created_at",
                                    "updated_at",
                                    "user_confirmed_at",
                                    "last_synced_at",
                                    "last_pushed_to_container_at",
                                )
                            },
                            "reason": "absent_from_complete_live_export",
                        }
                    )
                    row.delete()
                    del existing_rows[name]
        # Quarantine and promotion commit together; the signed selector never reads this field.
        versions = dict(record.get("imported_versions", {}))
        # Once imported, missing rows are dashboard tombstones, not invitations
        # to resurrect stale runtime truth. Updated rows belong to the user.
        known = set(versions)
        unchanged = {name for name, row in existing_rows.items() if versions.get(name) == row.updated_at.isoformat()}
        import_jobs = [j for j in jobs if j["name"] not in known or j["name"] in existing_rows]
        result = upsert_from_gateway_jobs(
            tenant,
            import_jobs,
            delete_missing=False,
            preserve_existing=tenant.postgres_cron_canonical,
        )
        # Re-capture user edits/cancellations only for rows owned by this import.
        upsert_from_gateway_jobs(tenant, [j for j in jobs if j["name"] in unchanged], delete_missing=False)
        prepare_writer_declarations(tenant)
        for row in CronJob.objects.filter(tenant=tenant, name__in=owned):
            if row.name not in versions or row.name in unchanged:
                versions[row.name] = row.updated_at.isoformat()
        # Commit ownership versions with the rows, so a crash cannot lose the
        # compare-and-swap baseline while leaving a completed import behind.
        Tenant.objects.filter(pk=tenant.pk).update(postgres_cron_canonical=True)
        _save(tenant, {**record, "imported_versions": versions, "quarantined_cache": quarantined})
    # Do not leak rolled-back ownership into the caller's failure checkpoint.
    record["imported_versions"] = versions
    record["quarantined_cache"] = quarantined
    tenant.postgres_cron_canonical = True
    # Automatic signed selection is unchanged. Unsupported exclusions were
    # refused before import; no migration-specific exemptions or resync waiver.
    record["needs_agent_resync"] = []
    _save(tenant, record)
    return {
        **result,
        "captured": len(jobs),
        "quarantined_cache": len(quarantined),
        "needs_agent_resync": [],
        "disabled_retained": CronJob.objects.filter(tenant=tenant, enabled=False).count(),
    }


def assert_cutover_safe(jobs):
    """Defer imminent one-shots without mutating jobs; recurrences may be missed."""
    from .migration_preservation import authored_at_ms as _at_fires_at_ms

    now = int(timezone.now().timestamp() * 1000)
    for job in jobs:
        if (job.get("schedule") or {}).get("kind") != "at" or not job.get("enabled", True):
            continue
        if (job.get("state") or {}).get("runningAtMs") or job.get("runningAtMs"):
            raise MigrationError("cron_running")
        due = _at_fires_at_ms(job)
        if due is None:
            raise MigrationError("cron_next_fire_unknown")
        if due <= now + 60 * 60 * 1000:
            raise MigrationError("cron_imminent")


def one_shot_dispositions(tenant, record):
    from .migration_preservation import authored_at_ms as _at_fires_at_ms
    from .migration_preservation import declaration_digest

    exports = [h["jobs"] for h in record.get("cron_export_history", [])] + [record.get("cron_export", [])]
    captured_by_id = {
        (j.get("id") or j.get("jobId"), declaration_digest(j)): j
        for jobs in exports
        for j in jobs
        if (j.get("schedule") or {}).get("kind") == "at"
    }
    captured = list(captured_by_id.values())
    canonical = dict(record.get("canonical_one_shots", {}))

    for declaration, _ in canonical_inventory(tenant):
        if (declaration.get("schedule") or {}).get("kind") == "at":
            canonical[declaration["id"] + ":" + declaration_digest(declaration)] = declaration
    captured += list(canonical.values())
    if not captured:
        return
    observed = runtime_operator.list_crons(tenant)
    rows = {r.name: str(r.pk) for r in CronJob.objects.filter(tenant=tenant)}
    now = int(timezone.now().timestamp() * 1000)
    dispositions = []
    for job in captured:
        # Historical source resolutions are hints only. Both current authorities
        # and delivery evidence below must independently justify the disposition.
        due = _at_fires_at_ms(job)
        matches = [
            j
            for j in observed
            if j.get("id") == (job.get("id") or job.get("jobId"))
            or j.get("name") == job["name"]
            or j.get("declarationKey") == "nbhd:" + rows.get(job["name"], "missing")
        ]
        delivered = any(
            _at_fires_at_ms(j) == due
            and (j.get("state") or {}).get("lastDelivered") is True
            and (j.get("state") or {}).get("lastRunAtMs", 0) >= (due or now)
            for j in matches
        )
        if job["name"] not in rows and not matches:
            disposition = "cancelled"
        elif not job.get("enabled", True):
            disposition = "disabled_retained"
        elif delivered:
            disposition = "delivered"
        elif (
            due
            and due > now
            and len(matches) == 1
            and matches[0].get("enabled", True)
            and _at_fires_at_ms(matches[0]) == due
        ):
            disposition = "pending_runtime"
        elif due and due <= now:
            disposition = "expired_undelivered"
        else:
            disposition = "pending_missing"
        dispositions.append(
            {
                "id": job.get("id") or job.get("jobId"),
                "disposition": disposition,
                "observed_at": timezone.now().isoformat(),
            }
        )
    record["one_shot_dispositions"] = dispositions
    if any(d["disposition"] == "expired_undelivered" for d in dispositions):
        raise MigrationError("one_shot_expired_undelivered")
    if any(d["disposition"] == "pending_missing" for d in dispositions):
        raise MigrationError("one_shot_pending_missing")


def wait_healthy(tenant, *, image=None, suffix=None, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            app = get_app(tenant)
            identity = (not image or _image(app) == image) and (not suffix or app.template.revision_suffix == suffix)
            ready = app.latest_ready_revision_name and app.latest_revision_name == app.latest_ready_revision_name
            if identity and ready:
                responses = [
                    requests.get(f"https://{tenant.container_fqdn}{path}", timeout=10)
                    for path in ("/proxy-health", "/healthz")
                ]
                if all(r.status_code == 200 for r in responses):
                    return {"revision": app.latest_ready_revision_name, "proxy_health": 200, "healthz": 200}
        except Exception:
            # Retry read-only probes, without logging tenant data or SDK credentials.
            pass
        time.sleep(5)
    raise MigrationError("Bounded revision/proxy-health/healthz wait expired")


def fence_and_drain(tenant, record):
    # No transaction or row lock spans the drain or any external operation.
    # Repeat the drain on pre-submit recovery: a crash may have interrupted it.
    if _owned_query(tenant, record).update(openclaw_migration_cron_fenced=True) != 1:
        raise MigrationError("migration_owner_fenced")
    tenant.openclaw_migration_cron_fenced = True
    time.sleep(30)
    _assert_owner(tenant, record)


def image_step(tenant, record):
    _assert_owner(tenant, record)
    evidence = record["evidence"]["preflight"]
    image, suffix = evidence["target_image"], evidence["revision_suffix"]
    app = get_app(tenant)
    if _image(app) != image or app.template.revision_suffix != suffix:
        if VERSION in _image(app):
            raise MigrationError("unexpected_target_revision")
        fence_and_drain(tenant, record)
        # Final source/canonical snapshot includes edits admitted before the fence.
        record["evidence"]["capture"] = capture(tenant, record)
        preservation_precheck(tenant, record["cron_export"])
        stage_signed_crons(tenant, record, checkpoint="signed_prestaged")
        record["recovery"] = {
            "instructions": RECOVERY,
            "resume_tag": record["tag"],
            "revision_suffix": suffix,
            "owner_token": record.get("owner_token"),
            "signed_digest": record["signed_prestaged"]["digest"],
        }
        record["image_submitted"] = True
        _save(tenant, record)
        _assert_owner(tenant, record)
        # Rebuild current truth and verify actual share bytes immediately before
        # submission. Repair changed/missing bytes with atomic publication.
        stage_signed_crons(tenant, record, checkpoint="signed_prestaged")
        assert_cutover_safe([j for j, _ in canonical_inventory(tenant)] + record.get("cron_export", []))
        capture_undo_point(tenant, record, app)
        azure_client.update_container_image(
            tenant.container_id, image, revision_suffix=suffix, operation_timeout=300, retrofit_storage=True
        )
    # If a process died after the Azure write, re-use that revision instead of
    # creating another revision and losing the just-restored ephemeral state.
    # Records created before the fence field existed can already point at the
    # submitted revision. Recovery must fence this path before health/staging too.
    if not tenant.openclaw_migration_cron_fenced:
        fence_and_drain(tenant, record)
    # Only after submission: 5.28 cannot read a 9.4 config, so a failed submit
    # must leave the 5.28 file in place. A 9.4 boot that raced this write
    # restarts and picks it up (the revision restart-loops until healthy).
    stage_94_config(tenant, record)
    health = wait_healthy(tenant, image=image, suffix=suffix)
    stage_signed_crons(tenant, record, checkpoint="signed_after_health")
    return health


def capture_undo_point(tenant, record, app):
    """Record what --rollback needs, once, before the first image submission."""
    if record.get("undo"):
        return
    source_revision = getattr(app, "latest_ready_revision_name", None)
    if not source_revision:
        raise MigrationError("undo_point_unavailable")
    record["undo"] = {
        "share_snapshot": azure_client.snapshot_tenant_share(str(tenant.id)),
        "source_revision": source_revision,
        "source_image": _image(app),
        "taken_at": timezone.now().isoformat(),
    }
    _save(tenant, record)


def rollback_tenant(tenant_id) -> dict:
    """Operator undo for a failed migration: back to the exact 5.28 runtime.

    Restores the share snapshot taken before the swap (9.4's first boot
    rewrites files 5.28 cannot read), redeploys the pre-swap revision's
    template, waits for health, then restores the DB version/tag. Postgres
    cron rows were never removed; the 5.28 reconciler re-pushes them.
    """
    tenant = Tenant.objects.get(pk=tenant_id)
    record = dict(tenant.openclaw_migration or {})
    undo = record.get("undo")
    if not undo:
        raise MigrationError("no_undo_point")
    if record.get("status") == "RUNNING":
        lease = parse_datetime(record.get("lease_until") or "")
        if lease and lease > timezone.now():
            raise MigrationError("migration_running")
    source = record["evidence"]["preflight"]["source"]
    Tenant.objects.filter(pk=tenant.pk).update(openclaw_migration_cron_fenced=True)
    restored = azure_client.restore_tenant_share(str(tenant.id), undo["share_snapshot"])
    suffix = "rb-" + uuid.uuid4().hex[:10]
    azure_client.copy_revision(tenant.container_id, undo["source_revision"], suffix)
    health = wait_healthy(tenant, image=undo["source_image"], suffix=suffix)
    Tenant.objects.filter(pk=tenant.pk).update(
        openclaw_version=source["version"], container_image_tag=source["tag"], openclaw_migration_cron_fenced=False
    )
    # A later run starts fresh; the attempt stays on file for audit.
    rolled_back = {
        "status": "ROLLED_BACK",
        "rolled_back_at": timezone.now().isoformat(),
        "rollback": {**restored, **health},
        "previous": record,
    }
    Tenant.objects.filter(pk=tenant.pk).update(openclaw_migration=rolled_back)
    return {"status": "ROLLED_BACK", "revision": health["revision"], **restored}


def stage_94_config(tenant, record):
    """Put a 9.4-rendered openclaw.json on the share before 9.4 boots from it.

    9.4 reads the share config at boot. A 5.28 render enables web search with
    a provider 9.4 must npm-install at boot, and that install cannot parse npm
    output under stdout redaction, so the gateway refuses to start and the
    revision restart-loops (E2E canary 2026-09-25). The 9.4 render uses the
    image-vendored provider. The later config step still does the strict full
    refresh; this only guarantees the first 9.4 boot reads a 9.4 config.
    """
    from .azure_client import upload_config_to_file_share
    from .config_generator import config_to_json, generate_openclaw_config

    _assert_owner(tenant, record)
    rendered = Tenant.objects.select_related("user").get(pk=tenant.pk)
    # Render only: the stored version/tag flip stays in the version step.
    rendered.openclaw_version, rendered.container_image_tag = VERSION, record["tag"]
    upload_config_to_file_share(str(rendered.id), config_to_json(generate_openclaw_config(rendered)))
    record["config_prestaged_at"] = timezone.now().isoformat()
    _save(tenant, record)


def stage_signed_crons(tenant, record, *, checkpoint):
    """Write/read back the exact canonical signed selection before image apply.

    5.28 ignores this file. 9.4's entrypoint installs it without the management
    process, including after process death immediately following image apply.
    """
    from apps.cron.share_cron_sync import build_signed_crons_doc

    from .migration_preservation import canonical_digests, reason_counts
    from .migration_signed_file import publish_signed_file

    _assert_owner(tenant, record)
    for _ in range(2):
        inventory = canonical_inventory(tenant)
        # Disabled rows stay in Postgres and are never signed (projected_canonical).
        reasons = reason_counts(projected_canonical(inventory))
        if reasons:
            raise PreservationError(reasons)
        if checkpoint == "signed_prestaged":
            assert_cutover_safe([j for j, _ in inventory] + record.get("cron_export", []))
        prepare_writer_declarations(tenant)
        revision = canonical_revision(tenant)
        data, count = build_signed_crons_doc(tenant)
        expected = canonical_digests(json.loads(json.loads(data)["signed"]))
        if expected != canonical_digests(_desired_jobs(tenant)) or revision != canonical_revision(tenant):
            continue
        _assert_owner(tenant, record)
        rewritten = publish_signed_file(tenant, data, before_publish=lambda: _assert_owner(tenant, record))
        observed = azure_client.download_workspace_file_binary(str(tenant.pk), "nbhd-crons.json")
        if observed != data:
            raise MigrationError("signed_file_readback_mismatch")
        if revision != canonical_revision(tenant) or expected != canonical_digests(_desired_jobs(tenant)):
            continue
        record[checkpoint] = {
            "digest": hashlib.sha256(data).hexdigest(),
            "canonical_digests": expected,
            "canonical_revision": revision,
            "count": count,
            "rewritten": rewritten,
            "at": timezone.now().isoformat(),
        }
        _save(tenant, record)
        return record[checkpoint]
    raise MigrationError("canonical_changed_during_prestaging")


def version_step(tenant, record):
    _assert_owner(tenant, record)
    if (
        _owned_query(tenant, record).update(
            container_image_tag=record["tag"], openclaw_version=VERSION, openclaw_migration_cron_fenced=False
        )
        != 1
    ):
        raise MigrationError("migration_owner_fenced")
    tenant.container_image_tag, tenant.openclaw_version = record["tag"], VERSION
    tenant.openclaw_migration_cron_fenced = False
    return {"tag": record["tag"], "version": VERSION}


def config_step(tenant, record):
    _assert_owner(tenant, record)
    # Stamp only the version being rendered; a concurrent pending bump stays pending.
    from django.db.models import F, Value
    from django.db.models.functions import Greatest

    from .services import update_tenant_config

    tenant.refresh_from_db()
    version = max(tenant.pending_config_version, tenant.config_version, 1)
    # Strict propagates workspace/USER.md failures normally tolerated by sweeps.
    with suppress_cronjob_reconcile():
        update_tenant_config(str(tenant.id), strict=True, refresh_crons=False)
    if (
        _owned_query(tenant, record).update(
            config_version=version, pending_config_version=Greatest(F("pending_config_version"), Value(version))
        )
        != 1
    ):
        raise MigrationError("migration_owner_fenced")
    # 9.4 does not see edits to its config on the SMB share (no file events),
    # so the regenerated file only takes effect on a restart.
    azure_client.restart_revision(tenant.container_id, get_app(tenant).latest_ready_revision_name)
    time.sleep(25)
    wait_healthy(tenant, timeout=300)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            observed = runtime_operator.config_observed(tenant)
            wait_healthy(tenant, timeout=30)
            return {"config_version": version, **observed}
        except Exception:
            time.sleep(5)
    raise MigrationError("Regenerated config was not observed by the gateway within 180 seconds")


def _inspection_clean(inspection, expected):
    return (
        _signed_match(inspection)
        and {m["key"] for m in inspection["matches"]} == set(expected)
        and not inspection.get("legacy", [])
    )


def _signed_match(inspection):
    return (
        not inspection["extras"]
        and len(inspection["matches"]) == inspection["expected"]
        and all(m["match"] and m["id"] for m in inspection["matches"])
    )


def prepare_writer_declarations(tenant):
    """Keep the unchanged signed writer stable after CLI timestamp normalization."""
    from .cron_reconcile import _row_to_cron_dict
    from .migration_preservation import declaration_digest, writer_stable_declaration

    with suppress_cronjob_reconcile(), transaction.atomic():
        for row in CronJob.objects.select_for_update().filter(tenant=tenant, enabled=True):
            before = declaration_digest(_row_to_cron_dict(row))
            stable = writer_stable_declaration(row.data)
            original = row.data
            row.data = stable
            if declaration_digest(_row_to_cron_dict(row)) != before:
                raise MigrationError("preparation_semantic_change")
            if stable != original:
                CronJob.objects.filter(pk=row.pk).update(data=stable, updated_at=timezone.now())


def crons_step(tenant, record):
    _assert_owner(tenant, record)
    from .migration_preservation import reason_counts

    reasons = reason_counts(projected_canonical(canonical_inventory(tenant)))
    if reasons:
        raise PreservationError(reasons)
    prepare_writer_declarations(tenant)
    _assert_owner(tenant, record)
    count = stage_signed_crons(tenant, record, checkpoint="signed_crons")["count"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        _assert_owner(tenant, record)
        inspection = runtime_operator.inspect_signed_crons(tenant, cleanup=True)
        if _signed_match(inspection):
            return {
                "signed_count": count,
                "inspection": inspection,
                "needs_agent_resync": record.get("needs_agent_resync", []),
            }
        time.sleep(5)
    raise MigrationError("Signed cron sync did not converge within 180 seconds")


def canonical_revision(tenant):
    import hashlib
    import json

    rows = list(
        CronJob.objects.filter(tenant=tenant)
        .order_by("id")
        .values("id", "updated_at", "name", "enabled", "managed", "source", "data")
    )
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def verify(tenant, record):
    from .migration_preservation import canonical_digests, reason_counts

    for attempt in range(2):
        snapshot = canonical_revision(tenant)
        expected = canonical_digests(_desired_jobs(tenant))
        polls = []
        mismatch = False
        audit_error = None
        for index in range(2):
            if index:
                time.sleep(25)
            try:
                one_shot_dispositions(tenant, record)
            except MigrationError as exc:
                audit_error = exc
            inspection = runtime_operator.inspect_signed_crons(tenant, canonical_digests=expected)
            if not index:
                # Right after the config restart the in-container writer is
                # still re-syncing (E2E canary: every job briefly mismatched,
                # then matched). Let it settle before the stability pair.
                deadline = time.monotonic() + 180
                while not _inspection_clean(inspection, expected) and time.monotonic() < deadline:
                    time.sleep(30)
                    inspection = runtime_operator.inspect_signed_crons(tenant, canonical_digests=expected)
            mismatch |= not _inspection_clean(inspection, expected)
            polls.append({m["key"]: m["id"] for m in inspection["matches"]})
        health = wait_healthy(tenant, image=record["evidence"]["preflight"]["target_image"], timeout=30)
        logs = runtime_operator.console_error_counts(tenant, since=timezone.now() - timedelta(minutes=5))
        if any(logs["errors"].values()):
            record["verification_failure"] = {"console": logs}
            raise MigrationError("Recent console logs contain migration/runtime errors")
        reasons = reason_counts(projected_canonical(canonical_inventory(tenant)))
        current_desired = canonical_digests(_desired_jobs(tenant))
        current = canonical_revision(tenant)
        if snapshot != current or expected != current_desired:
            if attempt == 0:
                continue
            raise MigrationError("canonical_changed_during_verification")
        if reasons:
            raise PreservationError(reasons)
        if audit_error:
            raise audit_error
        if mismatch:
            raise MigrationError("canonical_content_mismatch")
        if polls[0] != polls[1]:
            raise MigrationError("cron_ids_unstable")
        break
    return {"result": "PASS", "polls": polls, "poll_interval_seconds": 25, "health": health, "console": logs}


HANDLERS = dict(zip(STEPS, (preflight, capture, image_step, version_step, config_step, crons_step, verify)))


def verify_existing(tenant, record):
    """Read-only canary verification; never replace an already-9.4 image/config."""
    try:
        from collections import Counter

        from .migration_preservation import preservation_reasons

        canonical = canonical_inventory(tenant)
        pins = {
            j["declarationKey"]: {
                "kind": j.get("schedule", {}).get("kind"),
                **{k: j.get("schedule", {}).get(k) is not None for k in ("anchorMs", "staggerMs")},
            }
            for j, _ in canonical
        }
        findings = runtime_operator.preservation_inventory(tenant, canonical_pins=pins)
        for job, managed in projected_canonical(canonical):
            codes = sorted(preservation_reasons(job, managed=managed))
            if codes:
                findings.append({"key": job["declarationKey"], "reasons": codes})
        if findings:
            reasons = dict(Counter(code for finding in findings for code in finding["reasons"]))
            return {"status": "BLOCKED_UNSUPPORTED", "steps": [], "reasons": reasons, "declarations": findings}
        current_image = _image(get_app(tenant))
        expected = f"{settings.AZURE_ACR_SERVER}/nbhd-openclaw:{tenant.container_image_tag}"
        if tenant.openclaw_version != VERSION or current_image.split("@", 1)[0] != expected:
            raise MigrationError("Already-9.4 Azure image and DB version/tag disagree")
        runtime_operator.config_observed(tenant)
        evidence = copy.deepcopy(record)
        evidence.setdefault("evidence", {}).setdefault("preflight", {})["target_image"] = current_image
        result = verify(tenant, evidence)
        return {"status": "NOOP", "steps": [], "note": "Already on 9.4; verification PASS", "verification": result}
    except Exception as exc:
        reason = (
            str(exc)
            if isinstance(exc, (MigrationError, runtime_operator.OperatorError))
            else "verification_unavailable"
        )
        codes = {
            "Already-9.4 Azure image and DB version/tag disagree": "image_version_mismatch",
            "Operator cron metadata does not match Postgres desired set": "cron_mismatch",
            "Unmatched enabled legacy cron remains; duplicate cleanup requires operator review": "legacy_crons_unresolved",
            "Cron IDs changed across two polls 25 seconds apart": "cron_ids_unstable",
            "Recent console logs contain migration/runtime errors": "runtime_errors",
            "Bounded revision/proxy-health/healthz wait expired": "health_unavailable",
        }
        code = codes.get(reason, reason if re.fullmatch(r"[a-z_]+", reason) else "verification_unavailable")
        raise VerificationError(code) from None


def migrate_tenant(tenant_id, tag: str, *, dry_run=False, takeover=None, confirm_owner_dead=False) -> dict:
    if not re.fullmatch(r"2026\.9\.4-[0-9a-f]{7,40}", tag or ""):
        raise MigrationError("Target must be an immutable 2026.9.4-<sha> tag")
    candidate = Tenant.objects.get(pk=tenant_id)
    prior = candidate.openclaw_migration or {}
    if not dry_run:
        _check_lease(prior, takeover, confirm_owner_dead)
    if not dry_run and candidate.openclaw_version != VERSION and not prior.get("image_submitted"):
        readiness = report_tenant(candidate) if not takeover else {"status": "READY"}
        if readiness["status"] != "READY":
            return {**readiness, "steps": []}
    with transaction.atomic():
        tenant = Tenant.objects.select_for_update().select_related("user").get(pk=tenant_id)
        record = copy.deepcopy(tenant.openclaw_migration or {})
        # A rolled-back attempt is history, not a resumable run.
        rolled_back = [record] if record.get("status") == "ROLLED_BACK" else []
        if rolled_back:
            record = {}
        eligible = (
            not tenant.hibernated_at
            and tenant.status == Tenant.Status.ACTIVE
            and tenant.container_id
            and tenant.container_fqdn
        )
        if dry_run and not eligible:
            raise MigrationError("Dry-run refused: tenant must be awake, active and provisioned")
        if record and record.get("status") != "PASS" and record.get("tag") != tag:
            raise MigrationError("Existing migration targets another tag; resolve its record first")
        already_current = eligible and (
            record.get("status") == "PASS"
            or (
                not record
                and tenant.openclaw_version == VERSION
                and tenant.container_image_tag.startswith(VERSION + "-")
            )
        )
        if dry_run and already_current:
            return {"status": "DRY_RUN", "steps": [], "note": "Already on 9.4; verification required on execution"}
        if dry_run:
            return {
                "status": "DRY_RUN",
                "steps": [s for s in STEPS if s == "verify" or s not in record.get("completed", [])],
                "note": "No writes or external calls; ACR, live provenance and health checked on execution",
            }
        if not already_current:
            _check_lease(record, takeover, confirm_owner_dead)
            if not record:
                record = {"tag": tag, "started_at": timezone.now().isoformat(), "completed": [], "evidence": {}}
                if rolled_back:
                    record["rolled_back_attempts"] = rolled_back
            # Verification is a current observation, never a resumable mutation
            # checkpoint. A crash after verifying must not certify stale state.
            record["completed"] = [s for s in record.get("completed", []) if s != "verify"]
            if takeover:
                record.setdefault("takeovers", []).append(
                    {
                        "dead_owner": takeover,
                        "at": timezone.now().isoformat(),
                        "termination_confirmed_by_operator": True,
                    }
                )
            record["owner_token"] = uuid.uuid4().hex
            record["owner"] = {"host": socket.gethostname(), "pid": os.getpid()}
            record["status"] = "RUNNING"
            # Claim under SELECT FOR UPDATE. Later saves compare this fencing token.
            Tenant.objects.filter(pk=tenant.pk).update(openclaw_migration=record)
            _save(tenant, record)
    if already_current:
        return verify_existing(tenant, record)
    step = record.get("step", "preflight")
    try:
        for step in STEPS:
            if step in record["completed"]:
                continue
            tenant.refresh_from_db()
            if tenant.hibernated_at:
                raise MigrationError("Hibernated tenant refused: wake on its current image, then migrate")
            if tenant.status != Tenant.Status.ACTIVE or not tenant.container_id or not tenant.container_fqdn:
                raise MigrationError("Migration requires an active, provisioned tenant")
            record["step"] = step
            record.setdefault("step_started_at", {})[step] = timezone.now().isoformat()
            _save(tenant, record)
            evidence = HANDLERS[step](tenant, record)
            record["evidence"][step] = evidence
            record["completed"].append(step)
            record.setdefault("step_completed_at", {})[step] = timezone.now().isoformat()
            _save(tenant, record)
        record["status"] = "PASS"
        record["finished_at"] = timezone.now().isoformat()
        record.pop("failure", None)
        record.pop("verification_failure", None)
        _save(tenant, record)
        return {
            "status": "PASS",
            "steps": record["completed"],
            "needs_agent_resync": len(record.get("needs_agent_resync", [])),
            "quarantined_cache": len(record.get("quarantined_cache", [])),
        }
    except Exception as exc:
        record["status"] = "BLOCKED_UNSUPPORTED" if isinstance(exc, PreservationError) else "FAILED"
        if not record.get("image_submitted") and str(exc) in {
            "cron_imminent",
            "cron_running",
            "cron_next_fire_unknown",
        }:
            record["status"] = "DEFERRED"
        # SDK errors can embed URLs, tokens and output. Persist only safe failures.
        reason = str(exc) if isinstance(exc, (MigrationError, runtime_operator.OperatorError)) else type(exc).__name__
        record["failure"] = {"step": step, "reason": reason, "at": timezone.now().isoformat()}
        _save(tenant, record)
        raise MigrationError(f"FAILED at {step}: {reason}. {RECOVERY}") from None
