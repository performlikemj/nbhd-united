"""Async best-effort DEK pre-warm — encryption-at-rest Phase 1 (PR4).

Populates ``apps.crypto.cache`` with every provisioned tenant's DEK ahead of
the first real request, so the cold-start cost (one Key Vault unwrap per
tenant) happens at worker boot / poller start instead of stacking onto a
user's chat turn — or, once a later phase wires a real decrypt consumer,
onto that read. Phase 1 ships this DARK: nothing decrypts plaintext yet, so
a warm cache entry just sits there inert until Phase 2+ reads it.

Best-effort by design, mirroring the PII-model warm in ``gunicorn.conf.py``:
a Key Vault hiccup on one tenant must never abort the sweep, the sweep must
never crash the calling worker/poller, and it must never delay boot or the
container health check — it always runs on its own daemon thread, off the
request path entirely.
"""

from __future__ import annotations

import logging
import sys
import threading

from django.db import close_old_connections

from apps.crypto import cache

logger = logging.getLogger(__name__)

# The one manage.py subcommand allowed to warm. The central Telegram poller
# is started as `python manage.py poll_telegram` (see startup.sh) but is a
# long-running server process, not a one-shot management command — it holds
# fleet DEKs today for redaction mints and will decrypt contextual-recall in
# a later phase, so it needs the cache warmed the same as a gunicorn worker.
_SERVER_SUBCOMMAND = "poll_telegram"


def _is_management_command() -> bool:
    """True for any ``manage.py <subcommand>`` invocation other than the poller.

    Red-team finding 7: pre-warm must never run inside a one-shot management
    command (``migrate``, ``test``, ``makemigrations``, ``shell``, ...) — the
    ``tenant_deks`` table/columns or the KEK Key Vault RBAC may not exist yet
    during a deploy's ``migrate`` step, and a broker-unwrap attempt there
    would crash the deploy. Detected via ``sys.argv`` (not a Django runtime
    flag) so it's correct even before settings/apps finish configuring.
    """
    argv = sys.argv
    if not argv or not argv[0]:
        return False
    if not argv[0].endswith("manage.py"):
        return False  # gunicorn, or any other non-management entrypoint
    subcommand = argv[1] if len(argv) > 1 else ""
    return subcommand != _SERVER_SUBCOMMAND


def _candidate_tenants():
    """All provisioned tenants (container + identity exist), including hibernated.

    Mirrors ``migrate_tenants_to_per_tenant_keys._candidates()`` minus its
    ``status=ACTIVE`` and ``internal_api_key=""`` filters — those narrow to
    "not yet migrated ACTIVE tenants" for a one-time backfill. Pre-warm wants
    every tenant with a live container + identity right now, hibernated or
    not: hibernated tenants are exactly the wake-storm this dark warm exists
    to soften ahead of later phases. Deprovisioned/deleted tenants clear
    both fields (see ``deprovision_tenant``), so they drop out naturally
    without a status filter.
    """
    from apps.tenants.models import Tenant

    return Tenant.objects.exclude(container_id="").exclude(managed_identity_id="")


def prewarm_all_provisioned() -> None:
    """Populate the DEK cache for every provisioned tenant. Never raises.

    Hard no-op under a one-shot management command (see
    ``_is_management_command``). Otherwise iterates all provisioned tenants
    and warms each via ``cache.get_dek``, logging and continuing past any
    single tenant's failure (purged/unreachable KEK, missing ``tenant_deks``
    row, transient Key Vault throttling) — one bad tenant must never abort
    the sweep. Runs on the caller's thread; callers that need this
    off-thread use ``start_prewarm_thread``.
    """
    if _is_management_command():
        return

    try:
        tenants = list(_candidate_tenants())
    except Exception:
        logger.warning("prewarm: failed to list candidate tenants; skipping sweep", exc_info=True)
        return

    for tenant in tenants:
        try:
            cache.get_dek(tenant.id, 0)
        except Exception:
            logger.warning(
                "prewarm: DEK warm failed for tenant %s (will retry on next real request/sweep)",
                str(tenant.id)[:8],
                exc_info=True,
            )


def _prewarm_thread_target() -> None:
    """The daemon thread's entry point: run the sweep, then release the connection.

    Kept separate from ``prewarm_all_provisioned`` so that function stays a
    plain, directly-testable call with no connection-lifecycle side effects
    (tests call it synchronously on the test's own connection). This
    wrapper is only ever reached via a dedicated ``threading.Thread`` that
    does nothing else, exactly like the poller's own poll loop — so closing
    the connection here is safe and mirrors that same cleanup (see
    ``apps/router/poller.py``): without it, the connection this thread
    opened would sit around for the rest of the thread's life, pinning a
    Supavisor pool slot.
    """
    try:
        prewarm_all_provisioned()
    finally:
        close_old_connections()


def start_prewarm_thread() -> None:
    """Kick off ``prewarm_all_provisioned`` on a daemon thread. Never raises.

    Fire-and-forget: callers (gunicorn's ``post_worker_init``, the poller's
    ``start()``) must never block worker boot or the container health check
    on this, and a thread-spawn failure must degrade to "cache stays cold,
    same as before this feature existed" rather than taking the caller down.
    """
    try:
        threading.Thread(target=_prewarm_thread_target, daemon=True, name="dek-prewarm").start()
    except Exception:
        logger.warning("prewarm: failed to start pre-warm thread", exc_info=True)
