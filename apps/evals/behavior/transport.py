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

SCENARIO ISOLATION (the run-33 fix): the suite drives many scenarios against ONE
behavior tenant in a single fire. If they all shared one conversation, a later
scenario's judge scores and hard checks would be contaminated by earlier ones —
observed live on run 33, where the assistant referenced prior scenarios ("three-
for-three on boundary tests"). Each ChatThread maps to its OWN OpenClaw session
(``user="thread:<id>"`` — ``apps/router/chat_views.py::_thread_user_param``), and
OpenClaw holds the running transcript keyed by that session param (the control
plane does NOT replay DB history into the prompt). So the transport opens a FRESH
thread before each scenario (``open_conversation``) via the real ``POST
/chat/threads/`` "new chat" primitive — a fresh, empty transcript with no recap
bleed — and carries that thread's id on every ``send_turn``. This isolates the
CONVERSATION TRANSCRIPT (where the contamination lived). Tenant-wide memory
(USER.md / journal / crons / proactive-context) stays SHARED across scenarios by
design — a true container-side memory reset has no clean server-side entry point
today; the suite's fresh-per-run markers + time-windowed DB assertions keep a
run's HARD checks residue-immune regardless (see ``suites/behavior.py``).

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
# The "new chat" primitive — POST mints a fresh, non-main ChatThread and returns
# ``{"id": <uuid>, ...}``. One fresh thread per scenario is the isolation seam:
# each thread hashes to its own OpenClaw session (thread:<id>), so a fresh thread
# is a fresh, empty transcript. Same endpoint iOS uses — no new backend surface.
_THREADS_PATH = "/api/v1/chat/threads/"
# Content-free label for a per-scenario scope thread (stored + encrypted at rest);
# never carries scenario content — the scenario id namespaces the recorded rows.
_SCOPE_THREAD_TITLE = "eval-behavior-scope"
_TERMINAL_STATUSES = frozenset({"ready", "error"})
_HTTP_TIMEOUT_SECONDS = 15.0
# Warm-turn poll deadline. The chat probe's warm SLO is ~45s; 60s gives slack while
# keeping the SUITE's worst-case arithmetic inside the 300s gunicorn ceiling (see
# SUITE_BUDGET_SECONDS in apps/evals/suites/behavior.py — a 2-turn scenario must
# fit 180 + 60 + judge within the budget). A warm turn slower than 60s is exactly
# the sickness the suite should surface as a failed turn, not wait out.
DEFAULT_DEADLINE_SECONDS = 60.0
# The FIRST turn of a run may find the behavior tenant HIBERNATED — a cold start
# "regularly past 2 min" (apps/orchestrator/hibernation.py) — so it gets a
# wake-aware deadline aligned with the wake probe's SLO (~180s,
# docs/evals-wave-b-plan.md Probe 4). Every later turn is warm.
FIRST_TURN_DEADLINE_SECONDS = 180.0
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
    """Drives one scenario turn against the behavior tenant and returns the reply.

    ``deadline_seconds`` overrides the transport's default poll deadline for THIS
    turn — the suite passes the wake-aware ``FIRST_TURN_DEADLINE_SECONDS`` for the
    run's first turn and the default for the rest.
    """

    def open_conversation(self) -> str:
        """Open a FRESH conversation scope for the next scenario, make it active for
        subsequent ``send_turn`` calls, and return its (content-free) scope id. The
        suite calls this before EACH scenario so scenarios cannot see each other's
        transcript. MUST raise on failure — an un-openable scope means the run
        cannot guarantee isolation and must ERROR loudly (directive INVARIANT #3),
        never silently reuse a prior scenario's (contaminated) scope."""
        ...

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult: ...


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
        # The active per-scenario conversation scope (a ChatThread id). None until
        # the suite opens the first scope; ``send_turn`` carries it as ``thread_id``
        # so every turn lands in this scenario's own OpenClaw session.
        self._thread_id: str | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._pat}", "Content-Type": "application/json"}

    def open_conversation(self) -> str:
        """Mint a FRESH thread (its own OpenClaw session) and make it the active
        scope. Raises ``BehaviorConfigError`` on any failure so a scope we cannot
        open ERRORs the run loudly (INVARIANT #3) rather than silently reusing the
        prior scenario's contaminated scope."""
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
        threads_url = f"{self._base_url}{_THREADS_PATH}"
        try:
            try:
                resp = client.post(threads_url, json={"title": _SCOPE_THREAD_TITLE}, headers=self._headers())
            except httpx.HTTPError as exc:
                raise BehaviorConfigError(
                    "behavior transport: could not open a fresh conversation scope "
                    "(POST /chat/threads/ failed) — refusing to drive a scenario into a shared scope"
                ) from exc
            if resp.status_code not in (200, 201):
                raise BehaviorConfigError(
                    f"behavior transport: opening a fresh conversation scope returned HTTP {resp.status_code}"
                )
            try:
                body = resp.json()
            except (ValueError, TypeError) as exc:
                raise BehaviorConfigError(
                    "behavior transport: /chat/threads/ returned a non-JSON body — cannot scope the scenario"
                ) from exc
            thread_id = str((body or {}).get("id") or "").strip()
            if not thread_id:
                raise BehaviorConfigError(
                    "behavior transport: /chat/threads/ returned no thread id — cannot scope the scenario"
                )
            self._thread_id = thread_id
            return thread_id
        finally:
            if owns_client:
                client.close()

    def send_turn(self, *, text: str, deadline_seconds: float | None = None) -> TurnResult:
        client_msg_id = uuid.uuid4().hex
        result = TurnResult(user_text=text)
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
        post_url = f"{self._base_url}{_MESSAGES_PATH}"
        detail_url = f"{self._base_url}{_MESSAGES_PATH}{client_msg_id}/"
        # Carry the active scope's thread id so this turn lands in the scenario's
        # own OpenClaw session (transcript isolation). Absent only if a caller drove
        # a turn without opening a scope — the suite always opens one first.
        post_body = {"client_msg_id": client_msg_id, "text": text}
        if self._thread_id:
            post_body["thread_id"] = self._thread_id
        started = time.monotonic()
        turn_deadline = deadline_seconds if deadline_seconds is not None else self._deadline
        try:
            try:
                resp = client.post(post_url, json=post_body, headers=self._headers())
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

            deadline = started + turn_deadline
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


def resolve_behavior_pat(tenant) -> str:
    """Return the raw behavior PAT, or RAISE ``BehaviorConfigError``.

    Mirrors the journey precedent (``apps/evals/journey/chat_drive.py::
    resolve_journey_pat``): beyond "is it set", the PAT is the credential that
    actually drives scenario traffic, so we verify it is VALID and belongs to the
    SAME synthetic behavior tenant — refusing to drive behavior scripts through a
    real subscriber's assistant if the secret was mis-minted. A mismatch is a loud
    config failure, not a silent skip.
    """
    raw = getattr(settings, "EVAL_BEHAVIOR_PAT", "") or ""
    if not raw:
        raise BehaviorConfigError(
            "EVAL_BEHAVIOR_PAT is not set — cannot authenticate the behavior suite "
            "as the synthetic behavior tenant's user. This PR lands INERT; the ops "
            "provisioning step mints the PAT and sets the secret."
        )

    from apps.tenants.pat_models import PersonalAccessToken, hash_token

    try:
        pat = PersonalAccessToken.objects.select_related("user", "user__tenant").get(token_hash=hash_token(raw))
    except PersonalAccessToken.DoesNotExist as exc:
        raise BehaviorConfigError("EVAL_BEHAVIOR_PAT does not match any personal access token.") from exc
    if not pat.is_valid:
        raise BehaviorConfigError("EVAL_BEHAVIOR_PAT is revoked or expired.")
    pat_tenant = getattr(pat.user, "tenant", None)
    if pat_tenant is None or pat_tenant.id != tenant.id:
        raise BehaviorConfigError(
            "EVAL_BEHAVIOR_PAT belongs to a different tenant than EVAL_BEHAVIOR_TENANT_ID — "
            "refusing to drive behavior scenarios through the wrong account."
        )
    return raw


def build_behavior_transport(tenant) -> BehaviorTransport:
    """Build the REAL container transport for the behavior tenant.

    The PAT is resolved through ``resolve_behavior_pat`` (journey precedent):
    validity-checked and tenant-match ENFORCED, so a wrong-PAT config slip can
    never drive behavior scripts into a real subscriber's assistant.

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
    if not base_url:
        raise BehaviorConfigError(
            "DJANGO_BASE_URL is not set — the behavior suite cannot reach the "
            "control plane's own API. This PR lands INERT; fire-verification "
            "follows provisioning."
        )
    pat = resolve_behavior_pat(tenant)
    return HttpxBehaviorTransport(base_url=base_url, pat=pat)


# Scenario isolation is now REAL, via a fresh conversation scope per scenario
# (``BehaviorTransport.open_conversation`` above, called by the suite before each
# scenario). The retired ``reset_behavior_workspace`` no-op over-scoped the
# problem: the run-33 contamination was CONVERSATION-TRANSCRIPT bleed, which a
# fresh per-scenario OpenClaw session isolates cleanly with no container-side
# reset. The heavier, genuinely-deferred concern remains honest: tenant-wide
# container memory (USER.md, journal, the OpenClaw cron SQLite mirror) and DB rows
# (CronJob, ProactiveOutbound) still persist across scenarios — a TRUE memory
# reset has no clean server-side entry point today (deleting a CronJob row without
# the OC ``cron.remove`` lifecycle would desync the container's SQLite mirror,
# invariants.md §9 — a fake cleanup, not a reset). The suite's fresh-per-run
# markers + time-windowed DB assertions keep a run's HARD checks residue-immune
# regardless, so correctness never depends on that deferred reset.


def now() -> datetime:
    """Front-door timestamp (kept trivial so tests can freeze it if needed)."""
    return timezone.now()
