"""Explicit, image-first OpenClaw 9.4 migration with durable resume checkpoints.

Network operations always run outside DB transactions. The record is private:
its source cron export contains reminder payloads and must never be printed.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import time
import uuid
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.cron.models import CronJob
from apps.cron.share_cron_sync import _desired_jobs, write_tenant_crons_file
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


def _save(tenant, record):
    record["updated_at"] = timezone.now().isoformat()
    record["lease_until"] = (timezone.now() + timedelta(minutes=30)).isoformat()
    from .hibernation import _update_tenant_after_azure

    _update_tenant_after_azure(tenant.pk, openclaw_migration=record)
    tenant.openclaw_migration = copy.deepcopy(record)


def registry_digest(image: str) -> str:
    """ACR metadata only. Never include CLI stderr (which may contain credentials)."""
    if "@sha256:" in image:
        digest = image.rsplit("@", 1)[1]
    else:
        registry, reference = image.split("/", 1)
        result = subprocess.run(
            [
                "az",
                "acr",
                "repository",
                "show",
                "--name",
                registry.split(".")[0],
                "--image",
                reference,
                "--query",
                "digest",
                "--output",
                "json",
                "--only-show-errors",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise MigrationError("ACR digest check failed: image missing or registry permission unavailable")
        digest = json.loads(result.stdout)
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise MigrationError("ACR did not return a valid digest")
    return digest


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


def capture(tenant, record):
    from apps.cron.gateway_client import invoke_gateway_tool
    from apps.cron.postgres_canonical import upsert_from_gateway_jobs
    from apps.orchestrator.cron_reconcile import _complete_cron_observation, _is_unmanaged_cron

    # Persist the HTTP export BEFORE touching Postgres or creating any revision.
    if "cron_export" not in record:
        response = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        jobs = _complete_cron_observation(response)
        if jobs is None or not isinstance(jobs, list):
            raise MigrationError("Cron provenance uncertain: HTTP cron.list did not return a complete list")
        if any(not isinstance(j, dict) or not j.get("name") or not (j.get("id") or j.get("jobId")) for j in jobs):
            raise MigrationError("Cron provenance uncertain: unnamed or unidentified jobs")
        names = [j["name"] for j in jobs]
        if len(set(names)) != len(names):
            raise MigrationError("Cron provenance uncertain: duplicate names require operator review")
        record["cron_export"] = jobs
        record["cron_export_at"] = timezone.now().isoformat()
        record["cron_source"] = "live HTTP cron.list, includeDisabled=true; no fallback"
        _save(tenant, record)
    jobs = record["cron_export"]
    # Canonical rows win conflicts; the export remains available for recovery.
    # A noncanonical import cannot replace typed rows from a different authority.
    if not tenant.postgres_cron_canonical and CronJob.objects.filter(tenant=tenant, creation_path="typed").exists():
        raise MigrationError("Cron provenance uncertain: noncanonical tenant has typed Postgres rows")
    with suppress_cronjob_reconcile():
        result = upsert_from_gateway_jobs(
            tenant,
            jobs,
            delete_missing=False,
            preserve_existing=tenant.postgres_cron_canonical,
        )
    Tenant.objects.filter(pk=tenant.pk).update(postgres_cron_canonical=True)
    tenant.postgres_cron_canonical = True
    # Keep non-agent unmanaged rows in the signed desired set permanently. Agent
    # recurring jobs are explicitly handed back to their owner for re-sync.
    rows = list(CronJob.objects.filter(tenant=tenant))
    preserved = [r.pk for r in rows if r.source != "agent" and (not r.managed or _is_unmanaged_cron(r.name))]
    agent_names = {r.name for r in rows if r.source == "agent"}
    needs_resync = [
        j.get("id") or j.get("jobId")
        for j in jobs
        if j["name"] in agent_names and (j.get("schedule") or {}).get("kind") != "at"
    ]
    record["preserved_unmanaged_ids"] = [str(pk) for pk in preserved]
    record["needs_agent_resync"] = needs_resync
    _save(tenant, record)
    return {
        **result,
        "captured": len(jobs),
        "needs_agent_resync": needs_resync,
        "disabled_retained": CronJob.objects.filter(tenant=tenant, enabled=False).count(),
    }


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


def image_step(tenant, record):
    evidence = record["evidence"]["preflight"]
    image, suffix = evidence["target_image"], evidence["revision_suffix"]
    app = get_app(tenant)
    if _image(app) != image or app.template.revision_suffix != suffix:
        azure_client.update_container_image(tenant.container_id, image, revision_suffix=suffix, operation_timeout=300)
    # If a process died after the Azure write, re-use that revision instead of
    # creating another revision and losing the just-restored ephemeral state.
    return wait_healthy(tenant, image=image, suffix=suffix)


def version_step(tenant, record):
    Tenant.objects.filter(pk=tenant.pk).update(container_image_tag=record["tag"], openclaw_version=VERSION)
    tenant.container_image_tag, tenant.openclaw_version = record["tag"], VERSION
    return {"tag": record["tag"], "version": VERSION}


def config_step(tenant, record):
    # Stamp only the version being rendered; a concurrent pending bump stays pending.
    from django.db.models import F, Value
    from django.db.models.functions import Greatest

    from .services import update_tenant_config

    tenant.refresh_from_db()
    version = max(tenant.pending_config_version, tenant.config_version, 1)
    # Strict propagates workspace/USER.md failures normally tolerated by sweeps.
    with suppress_cronjob_reconcile():
        update_tenant_config(str(tenant.id), strict=True, refresh_crons=False)
    Tenant.objects.filter(pk=tenant.pk).update(
        config_version=version, pending_config_version=Greatest(F("pending_config_version"), Value(version))
    )
    time.sleep(25)  # Allow the runtime file watcher and any config-triggered restart to settle.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            observed = runtime_operator.config_observed(tenant)
            wait_healthy(tenant, timeout=30)
            return {"config_version": version, **observed}
        except Exception:
            time.sleep(5)
    raise MigrationError("Regenerated config was not observed by the gateway within 180 seconds")


def _signed_match(inspection):
    return (
        not inspection["extras"]
        and len(inspection["matches"]) == inspection["expected"]
        and all(m["match"] and m["id"] for m in inspection["matches"])
    )


def crons_step(tenant, record):
    count = write_tenant_crons_file(tenant)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        inspection = runtime_operator.inspect_signed_crons(tenant, cleanup=True)
        if _signed_match(inspection):
            return {
                "signed_count": count,
                "inspection": inspection,
                "needs_agent_resync": record.get("needs_agent_resync", []),
            }
        time.sleep(5)
    raise MigrationError("Signed cron sync did not converge within 180 seconds")


def verify(tenant, record):
    expected_keys = {j["declarationKey"] for j in _desired_jobs(tenant)}
    polls = []
    for index in range(2):
        if index:
            time.sleep(25)
        inspection = runtime_operator.inspect_signed_crons(tenant)
        if not _signed_match(inspection) or {m["key"] for m in inspection["matches"]} != expected_keys:
            record["verification_failure"] = {"inspection": inspection, "expected_keys": sorted(expected_keys)}
            raise MigrationError("Operator cron metadata does not match Postgres desired set")
        if set(inspection.get("legacy", [])) - set(record.get("needs_agent_resync", [])):
            record["verification_failure"] = {"inspection": inspection}
            raise MigrationError("Unmatched enabled legacy cron remains; duplicate cleanup requires operator review")
        polls.append({m["key"]: m["id"] for m in inspection["matches"]})
    if polls[0] != polls[1]:
        record["verification_failure"] = {"polls": polls}
        raise MigrationError("Cron IDs changed across two polls 25 seconds apart")
    health = wait_healthy(tenant, image=record["evidence"]["preflight"]["target_image"], timeout=30)
    logs = runtime_operator.console_error_counts(tenant, since=timezone.now() - timedelta(minutes=5))
    if any(logs["errors"].values()):
        record["verification_failure"] = {"console": logs}
        raise MigrationError("Recent console logs contain migration/runtime errors")
    return {"result": "PASS", "polls": polls, "poll_interval_seconds": 25, "health": health, "console": logs}


HANDLERS = dict(zip(STEPS, (preflight, capture, image_step, version_step, config_step, crons_step, verify)))


def verify_existing(tenant, record):
    """Read-only canary verification; never replace an already-9.4 image/config."""
    try:
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
        reason = str(exc) if isinstance(exc, (MigrationError, runtime_operator.OperatorError)) else type(exc).__name__
        raise MigrationError(f"Already on 9.4; verification FAILED: {reason}. {RECOVERY}") from None


def migrate_tenant(tenant_id, tag: str, *, dry_run=False) -> dict:
    if not re.fullmatch(r"2026\.9\.4-[0-9a-f]{7,40}", tag or ""):
        raise MigrationError("Target must be an immutable 2026.9.4-<sha> tag")
    with transaction.atomic():
        tenant = Tenant.objects.select_for_update().select_related("user").get(pk=tenant_id)
        record = copy.deepcopy(tenant.openclaw_migration or {})
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
                "steps": [s for s in STEPS if s not in record.get("completed", [])],
                "note": "No writes or external calls; ACR, live provenance and health checked on execution",
            }
        if not already_current:
            lease = parse_datetime(record.get("lease_until", ""))
            if record.get("status") == "RUNNING" and lease and lease > timezone.now():
                raise MigrationError("Migration already running; wait for its 30-minute lease to expire")
            if not record:
                record = {"tag": tag, "started_at": timezone.now().isoformat(), "completed": [], "evidence": {}}
            record["status"] = "RUNNING"
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
        }
    except Exception as exc:
        record["status"] = "FAILED"
        # SDK errors can embed URLs, tokens and output. Persist only safe failures.
        reason = str(exc) if isinstance(exc, (MigrationError, runtime_operator.OperatorError)) else type(exc).__name__
        record["failure"] = {"step": step, "reason": reason, "at": timezone.now().isoformat()}
        _save(tenant, record)
        raise MigrationError(f"FAILED at {step}: {reason}. {RECOVERY}") from None
