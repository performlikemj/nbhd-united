"""DecryptAudit — encryption-at-rest Phase 1 (PR3).

A dedicated stdout JSON logger (`nbhd.decrypt_audit`), NOT a DB table. This
is deliberate (red-team findings 6/17): a DB-resident audit trail is
rewritable by anyone with DB creds, including an operator debugging a live
incident. A structured stdout log line ships to Log Analytics
(`ContainerAppConsoleLogs_CL`, workspace `035a49db-1da5-452d-8b32-b074d7a5d606`)
where DB access alone can't touch it. The guarantee this buys is "the event
carries no plaintext by construction" (callers only ever pass counts/ids,
never decrypted content) — not tamper-evidence via a scrubber; Django stdout
here isn't wrapped by the container-side `redact-stdout.js` scrubber, that's
OpenClaw-container-only.

Fires ONLY for a human-initiated read (`admin` console, or an owner
explicitly requesting their own decrypted content). `system` / `system_cron`
/ `runtime_endpoint` decrypts are the service doing its normal job (composing
a reply, running a scheduled task) — auditing those would drown the real
signal in noise for zero security value, so they're silent by design.

Principal boundaries (who sets what, and why the wiring is where it is):
  - Every request enters as `system`: ``TenantContextMiddleware.process_request``
    sets it UNCONDITIONALLY at entry (before auth) and ``process_response``
    calls ``reset_principal()`` at teardown — same shape as ``set_rls_context``
    / ``reset_rls_context``. Without the unconditional set + reset, a principal
    from one request would bleed into the next request served on the same
    reused gunicorn worker thread (a stale `owner_request` silently
    false-auditing an unrelated later read).
  - A subscriber authenticating upgrades the request to `owner_request`:
    ``JWTAuthenticationWithRLS`` and ``PersonalAccessTokenAuthentication`` set
    it right after ``set_rls_context`` — any synchronous decrypt under an
    authenticated subscriber is an owner-initiated read of their own data. For
    an owner reading MANY rows, prefer ``box.decrypt_bulk(..., principal="owner_request")``
    (one audit event for the batch) over N single decrypts.
  - QStash task dispatch (`cron.views.trigger_task`) sets `system_cron` — silent.
  - `admin` is intentionally UNWIRED today: there is no admin read path for the
    encrypted columns (they aren't registered on any AdminSite). It slots in
    later — at a PAT admin scope check, or a custom AdminSite boundary — when
    such a path first exists.
  - Background threads (``threading.Thread`` / ``ThreadPoolExecutor``) do NOT
    inherit the spawning request's principal — a fresh thread starts with a new
    context where the ContextVar holds its `system` default. That is correct BY
    DESIGN: a human did not perform a read that happens on a background worker,
    so it audits as `system` (silent), not as whatever principal spawned it.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar

from django.utils import timezone

_AUDIT = logging.getLogger("nbhd.decrypt_audit")

_PRINCIPAL: ContextVar[str] = ContextVar("decrypt_principal", default="system")

# The only principals a decrypt audit event fires for. Everything else
# (system, system_cron, runtime_endpoint, or any future value) is silent.
_AUDITED_PRINCIPALS = frozenset({"admin", "owner_request"})


def set_principal(kind: str) -> None:
    """Set the current context's decrypt principal.

    Call this once at the request/task boundary (e.g. the RLS-context setup
    for an admin console view, or the entry point of an owner-initiated
    "export my data" flow) with one of: "admin", "owner_request",
    "system_cron", "runtime_endpoint", "system". Everything decrypted for
    the remainder of this context is attributed to `kind` until changed.
    """
    _PRINCIPAL.set(kind)


def reset_principal() -> None:
    """Reset the current context's decrypt principal back to "system".

    Call at the request/task teardown boundary (``TenantContextMiddleware.
    process_response``, next to ``reset_rls_context``) so a principal set for
    one request can never bleed into the next request served on the same reused
    worker thread — the identical stale-context hazard ``reset_rls_context``
    guards against. Belt to ``set_principal``'s braces: ``process_request`` also
    sets "system" up front, so a missed reset is still corrected at the next
    request's entry.
    """
    _PRINCIPAL.set("system")


def get_principal() -> str:
    """Return the current context's decrypt principal (defaults to "system")."""
    return _PRINCIPAL.get()


def emit(
    tenant_id: object,
    table: str,
    column: str,
    row_count: int,
    principal_override: str | None = None,
) -> None:
    """Log a decrypt-audit event if the effective principal is human-initiated.

    No-op for `system`/`system_cron`/`runtime_endpoint` (or anything else
    outside `_AUDITED_PRINCIPALS`) — see module docstring. Never includes
    decrypted content, only identifying/counting metadata.

    `principal_override` attributes THIS event to a given principal WITHOUT
    touching the shared, process-lived `_PRINCIPAL` ContextVar. That matters
    for `decrypt_bulk`, which takes a per-call `principal=` argument: mutating
    the ambient var there would leak across calls on a reused worker thread —
    a later `system` decrypt would false-audit as the last bulk principal, and
    a bulk call defaulting to `system` would silence a genuinely-ambient
    `admin` read. A one-shot override sidesteps both.
    """
    principal = principal_override or _PRINCIPAL.get()
    if principal not in _AUDITED_PRINCIPALS:
        return

    _AUDIT.info(
        json.dumps(
            {
                "tenant_id": str(tenant_id),
                "table": table,
                "column": column,
                "row_count": row_count,
                "principal": principal,
                "ts": timezone.now().isoformat(),
            }
        )
    )
