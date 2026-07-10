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


def get_principal() -> str:
    """Return the current context's decrypt principal (defaults to "system")."""
    return _PRINCIPAL.get()


def emit(tenant_id: object, table: str, column: str, row_count: int) -> None:
    """Log a decrypt-audit event if the current principal is human-initiated.

    No-op for `system`/`system_cron`/`runtime_endpoint` (or anything else
    outside `_AUDITED_PRINCIPALS`) — see module docstring. Never includes
    decrypted content, only identifying/counting metadata.
    """
    principal = _PRINCIPAL.get()
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
