"""Detect and reap orphaned tenant container apps.

An *orphan* is an ``oc-*`` Azure Container App with no corresponding Tenant
row in the database. They arise when a Tenant row is deleted without
``deprovision_tenant`` completing — most commonly a User account deletion
(``Tenant.user`` is ``on_delete=CASCADE``) where the container teardown was
blocked by the production resource-group ``CanNotDelete`` lock (the failure is
swallowed by the account-deletion path) and the row then cascade-vanished.

An awake orphan keeps a replica running (compute cost) and POSTs internal
requests that fail auth (log noise), so the reaper:

  1. HIBERNATES awake orphans — deactivating revisions is NOT a delete, so it
     works under the prod locks and immediately stops cost + auth-failure spam.
  2. Optionally (``apply=True``) tears the orphan down fully — container app,
     env storage + file share, managed identity. These deletes are blocked by
     the ``no-delete-*`` prod locks unless an operator lifts them first; the
     reaper reports each resource as deleted / blocked / error rather than
     failing hard.
  3. Alerts the operator when any orphan is found.

Prevention lives in ``apps/tenants/signals.py`` (a ``pre_delete`` hook that
hibernates the container the instant a Tenant row is deleted); this module is
the periodic backstop + the operator's teardown tool.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_OC_PREFIX = "oc-"


def _is_lock_error(exc: Exception) -> bool:
    """Heuristic: did this Azure error come from a CanNotDelete lock?"""
    text = f"{type(exc).__name__}: {exc}"
    return "ScopeLocked" in text or "is locked" in text or "scope(s) are locked" in text


def _tenant_prefix(container_name: str) -> str:
    """The ``oc-<prefix>`` suffix — equals ``str(tenant.id)[:20]`` by convention."""
    return container_name[len(_OC_PREFIX) :] if container_name.startswith(_OC_PREFIX) else container_name


def find_orphaned_container_names() -> list[str]:
    """Return ``oc-*`` container app names that have no matching Tenant row."""
    from apps.orchestrator.azure_client import list_tenant_container_app_names
    from apps.tenants.models import Tenant

    live = list_tenant_container_app_names()
    if not live:
        return []

    # A tenant "owns" a container if its container_id matches the name, or its
    # id's 20-char prefix matches the name suffix (the oc-<id[:20]> convention,
    # a fallback for rows where container_id was never persisted).
    container_ids: set[str] = set()
    id_prefixes: set[str] = set()
    for cid, tid in Tenant.objects.values_list("container_id", "id"):
        if cid:
            container_ids.add(cid)
        id_prefixes.add(str(tid)[:20])

    orphans: list[str] = []
    for name in live:
        if name in container_ids:
            continue
        if _tenant_prefix(name) in id_prefixes:
            continue
        orphans.append(name)
    return orphans


def _teardown_orphan(container_name: str) -> dict[str, str]:
    """Best-effort full teardown of one orphan's Azure resources. Lock-tolerant.

    Returns ``{resource: status}`` where status is
    ``"deleted"`` | ``"attempted"`` | ``"blocked"`` | ``"error"``.
    """
    from apps.orchestrator import azure_client

    prefix = _tenant_prefix(container_name)
    result: dict[str, str] = {}

    # 1. Container app (delete_container_app RAISES on failure, incl. lock).
    try:
        azure_client.delete_container_app(container_name)
        result["container"] = "deleted"
    except Exception as exc:  # noqa: BLE001 — classify and continue
        result["container"] = "blocked" if _is_lock_error(exc) else "error"
        logger.warning("orphan_reaper: container delete %s -> %s", container_name, result["container"])

    # 2. Env storage binding + file share. delete_tenant_file_share only uses
    #    str(tenant_id)[:20], so passing the prefix yields the right ws-* name.
    try:
        azure_client.delete_tenant_file_share(prefix)
        result["file_share"] = "attempted"
    except Exception as exc:  # noqa: BLE001
        result["file_share"] = "blocked" if _is_lock_error(exc) else "error"

    # 3. Managed identity (mi-nbhd-<prefix>).
    try:
        azure_client.delete_managed_identity(prefix)
        result["managed_identity"] = "attempted"
    except Exception as exc:  # noqa: BLE001
        result["managed_identity"] = "blocked" if _is_lock_error(exc) else "error"

    return result


def reap_orphaned_containers(*, hibernate: bool = True, apply: bool = False, alert: bool = True) -> dict:
    """Find orphaned containers; hibernate awake ones; optionally tear down.

    Args:
        hibernate: deactivate awake orphans (lock-safe; stops cost + auth spam).
            Set False for a pure dry-run report.
        apply: additionally attempt full teardown (blocked by prod locks unless
            lifted; reported per-resource).
        alert: send one admin alert when any orphan is found.
    """
    from apps.orchestrator.azure_client import (
        container_app_has_active_revision,
        hibernate_container_app,
    )

    orphans = find_orphaned_container_names()
    summary: dict = {
        "orphans": orphans,
        "awake": [],
        "hibernated": [],
        "torn_down": {},
        "errors": [],
    }

    for name in orphans:
        try:
            awake = container_app_has_active_revision(name)
        except Exception:
            logger.exception("orphan_reaper: failed to read revision state for %s", name)
            summary["errors"].append(name)
            awake = False

        if awake:
            summary["awake"].append(name)
            if hibernate:
                try:
                    hibernate_container_app(name)
                    summary["hibernated"].append(name)
                except Exception:
                    logger.exception("orphan_reaper: failed to hibernate %s", name)
                    summary["errors"].append(name)

        if apply:
            summary["torn_down"][name] = _teardown_orphan(name)

    if orphans:
        logger.warning(
            "orphan_reaper: %d orphaned container(s) with no Tenant row: %s (awake=%d, hibernated=%d, apply=%s)",
            len(orphans),
            ", ".join(orphans),
            len(summary["awake"]),
            len(summary["hibernated"]),
            apply,
        )
        if alert:
            _alert(summary, apply=apply)
    else:
        logger.info("orphan_reaper: no orphaned containers found")

    return summary


def _alert(summary: dict, *, apply: bool) -> None:
    """Best-effort admin alert. Never raises."""
    try:
        from apps.cron.views import _send_alert_to_personal_openclaw

        lines = [
            f"⚠️ Orphaned-container reaper found {len(summary['orphans'])} container(s) with no Tenant row:",
            *[f"  • {n}" for n in summary["orphans"]],
            f"Awake: {len(summary['awake'])} → hibernated {len(summary['hibernated'])}.",
        ]
        if not apply:
            lines.append(
                "Full teardown is blocked by the prod CanNotDelete locks. "
                "After lifting the relevant lock, run: "
                "manage.py reap_orphaned_containers --apply"
            )
        _send_alert_to_personal_openclaw("\n".join(lines))
    except Exception:
        logger.exception("orphan_reaper: failed to send admin alert")


def reap_orphaned_containers_task() -> dict:
    """QStash cron entrypoint: detect + hibernate + alert (no destructive delete).

    Deletion is intentionally NOT automated here: the prod CanNotDelete locks
    block it, and auto-deleting container apps from a cron is risky. The cron's
    job is to ensure no orphan stays awake (cost/noise) and to surface orphans
    to the operator, who runs ``--apply`` after lifting the lock.
    """
    return reap_orphaned_containers(hibernate=True, apply=False, alert=True)
