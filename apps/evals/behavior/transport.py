"""Behavior-suite transport — drives multi-turn scenarios and reads the reply TEXT.

The behavior suite differs from the journey probes in one crucial way: the journey
chat driver reads only status METADATA and drops the reply text, because a journey
probe never needs the content. A behavior scenario DOES need the reply text — to
run the deterministic hard assertions (does the marker survive? was a boundary
held?) and to feed the LLM judge. That text is SYNTHETIC scenario content (no real
user), so it may flow to the assertions and the judge — but it MUST NOT be written
to ``EvalResult.details`` or any log line (INVARIANT #1). The suite keeps it in
memory only; nothing here persists or logs a reply.

The transport is an injectable seam: tests pass a fake that returns scripted
replies; production uses ``HttpxBehaviorTransport`` over the real chat path. The
production factory (``build_behavior_transport``) is a NAMED DEFERRAL until the
behavior tenant is provisioned — see its docstring.

INVARIANT #8: every httpx call here runs OUTSIDE any transaction (the suite opens
no ``atomic()`` around driving; it writes EvalResult rows only after a turn returns).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx
from django.conf import settings
from django.utils import timezone

from apps.evals.behavior.targets import BehaviorConfigError

logger = logging.getLogger(__name__)

_MESSAGES_PATH = "/api/v1/chat/messages/"
_TERMINAL_STATUSES = frozenset({"ready", "error"})
_HTTP_TIMEOUT_SECONDS = 15.0
DEFAULT_DEADLINE_SECONDS = 90.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0


@dataclass
class TurnResult:
    """One driven turn. ``reply_text`` is SYNTHETIC content — used by assertions +
    judge, NEVER stored in details or logged (INVARIANT #1)."""

    user_text: str
    reply_text: str = ""
    ok: bool = False
    # A short machine code for a failed turn (never content), e.g. "post_http_500".
    error: str = ""


@dataclass
class ScenarioRun:
    """Aggregate of one scenario's driven turns, plus the metadata the hard
    assertions need (the planted marker + the pre-drive timestamp for
    time-windowed DB queries)."""

    scenario_id: str
    marker: str
    started_at: datetime
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def replies(self) -> list[str]:
        return [t.reply_text for t in self.turns]

    @property
    def all_turns_ok(self) -> bool:
        return bool(self.turns) and all(t.ok for t in self.turns)


class BehaviorTransport(Protocol):
    """Drives one scenario turn against the behavior tenant and returns the reply."""

    def send_turn(self, *, text: str) -> TurnResult: ...


class HttpxBehaviorTransport:
    """Real transport: POST a turn on the chat path, poll to terminal, read the reply.

    Mirrors the journey chat driver's POST + poll idiom, but reads the ``reply_text``
    field (which the journey driver deliberately drops). The reply is synthetic
    scenario content — kept in memory for assertions + judge, never persisted or
    logged. A network/HTTP failure returns ``ok=False`` with a short code rather
    than raising, so one broken turn becomes a failed hard assertion (a clean run
    FAIL with a code), not a run ERROR.
    """

    def __init__(
        self,
        *,
        base_url: str,
        pat: str,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._pat = pat
        self._deadline = deadline_seconds
        self._poll_interval = poll_interval_seconds
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._pat}", "Content-Type": "application/json"}

    def send_turn(self, *, text: str) -> TurnResult:
        client_msg_id = uuid.uuid4().hex
        result = TurnResult(user_text=text)
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
        post_url = f"{self._base_url}{_MESSAGES_PATH}"
        detail_url = f"{self._base_url}{_MESSAGES_PATH}{client_msg_id}/"
        started = time.monotonic()
        try:
            try:
                resp = client.post(
                    post_url, json={"client_msg_id": client_msg_id, "text": text}, headers=self._headers()
                )
            except httpx.HTTPError:
                result.error = "post_error"
                logger.warning("behavior transport: POST failed")
                return result
            if resp.status_code not in (200, 201):
                result.error = f"post_http_{resp.status_code}"
                return result

            body = resp.json()
            if str(body.get("status") or "") in _TERMINAL_STATUSES:
                return self._finalize(result, body)

            deadline = started + self._deadline
            while time.monotonic() < deadline:
                time.sleep(self._poll_interval)
                try:
                    resp = client.get(detail_url, headers=self._headers())
                except httpx.HTTPError:
                    continue
                if resp.status_code != 200:
                    continue
                body = resp.json()
                if str(body.get("status") or "") in _TERMINAL_STATUSES:
                    return self._finalize(result, body)
            result.error = "timeout"
            return result
        finally:
            if owns_client:
                client.close()

    @staticmethod
    def _finalize(result: TurnResult, body: dict) -> TurnResult:
        status = str(body.get("status") or "")
        error = str(body.get("error") or "")
        if status == "ready" and not error:
            # reply_text: synthetic scenario content, kept in memory only.
            result.reply_text = str(body.get("reply_text") or "")
            result.ok = True
        else:
            result.error = error or status or "no_reply"
        return result


def build_behavior_transport(tenant) -> BehaviorTransport:
    """Build the REAL container transport for the behavior tenant.

    NAMED DEFERRAL (this PR lands INERT; docs/evals-directive.md §Suite 2). The
    behavior tenant is not provisioned yet, and no behavior PAT secret is wired, so
    this raises loudly rather than faking a transport. The driving logic itself is
    real (``HttpxBehaviorTransport``, unit-tested via an injected httpx client);
    only the credential/URL wiring is deferred to the ops provisioning step, which
    must create the container and set ``DJANGO_BASE_URL`` + an ``EVAL_BEHAVIOR_PAT``
    secret. Referencing ``EVAL_BEHAVIOR_PAT`` via ``getattr`` (not a new
    ``config/settings`` ``env()`` line) keeps Wave D a zero-settings-change PR; the
    provisioning step adds the setting + secret when it lands.
    """
    base_url = (getattr(settings, "DJANGO_BASE_URL", "") or "").rstrip("/")
    pat = getattr(settings, "EVAL_BEHAVIOR_PAT", "") or ""
    if not base_url or not pat:
        raise BehaviorConfigError(
            "Behavior transport is not wired yet — provisioning must create the "
            "behavior container and set EVAL_BEHAVIOR_PAT (+ DJANGO_BASE_URL). "
            "This PR lands INERT; fire-verification follows provisioning."
        )
    return HttpxBehaviorTransport(base_url=base_url, pat=pat)


def reset_behavior_workspace(tenant, run: ScenarioRun) -> None:
    """Reset the behavior tenant's workspace between scenario runs.

    NAMED DEFERRAL. A behavior scenario mutates the tenant's container memory
    (USER.md, journal, the OpenClaw cron SQLite mirror) and DB rows (CronJob,
    ProactiveOutbound). A TRUE reset — clearing container memory + the file share,
    and removing OC-side crons via the ``cron.remove`` lifecycle (invariants.md §9)
    — is a CONTAINER-SIDE operation with no clean server-side entry point today, so
    it is deferred until the behavior tenant is provisioned with a reset hook.

    What IS honestly feasible server-side is already done elsewhere: scenarios use a
    FRESH per-run marker and hard assertions use TIME-WINDOWED DB queries (see
    ``assertions.py``), so a single run's checks never read a prior run's residue —
    correctness of one run does not depend on this reset. We deliberately do NOT
    delete CronJob rows here: deleting the DB row without the OC ``cron.remove``
    lifecycle would desync the container's SQLite mirror (invariants.md §9) — a fake
    cleanup that looks like a reset but isn't. Left as a documented no-op rather than
    a fake implementation.
    """
    # Intentionally a documented no-op — see docstring (NAMED DEFERRAL). ``run`` is
    # accepted so a future container-side reset can scope to this run's mutations.
    _ = (tenant, run)
    return None


def now() -> datetime:
    """Front-door timestamp (kept trivial so tests can freeze it if needed)."""
    return timezone.now()
