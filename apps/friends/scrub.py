"""The fail-closed share scrub — the single egress class for lesson content
leaving a tenant (design §4.2/§4.3).

THE WHOLE POINT IS FAIL-CLOSED. ``apps/pii/engine.get_pii_pipeline`` caches a
load failure and re-raises it, and ``apps/pii/redactor._detect_pii`` SWALLOWS
that (``except Exception: model_results = []``) and silently continues with
Presidio-only recognizers — which have NO PERSON recognizer, so real names pass
through. A bare try/except around redaction therefore does NOT protect us: the
fallback succeeds while leaking.

So this module VERIFIES the DeBERTa NER path actually ran before trusting any
output: it calls ``get_pii_pipeline()`` directly (raising if the model is
unavailable / error-cached) and probes it with a known-name string, requiring a
PERSON detection. Anything short of that → ``scrub_status="failed"``, never
``ready``. Then it redacts with the owner's ``RedactionSession`` and neutralizes
every placeholder to a generic word via ``copilot._scrub_placeholders`` — NO
rehydration map is ever attached, so the recipient is structurally unable to
un-scrub.

The 554MB model must NOT be loaded in tests — patch ``get_pii_pipeline`` (or the
``_assert_ner_available`` / ``_redact_identities`` seams).
"""

from __future__ import annotations

import hashlib
import logging
import re

logger = logging.getLogger(__name__)

# Bump on a NER model upgrade so a re-scrub sweep re-verifies every snapshot.
SCRUB_MODEL_VERSION = "deberta_finetuned_pii/v1"

# A known-PII probe. If the NER pass can't find the PERSON here, it isn't the
# DeBERTa model (or it didn't run) — treat that exactly like a hard failure.
_NER_PROBE = "Michael Johnson met Sarah Lee at the Kyoto office on Tuesday."

# Conservative tag allowlist — lowercase simple slugs only; drop anything with
# capitals (proper nouns), punctuation, or length that could carry identity.
_SAFE_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,29}$")


class NerUnavailable(RuntimeError):
    """The DeBERTa NER path could not be verified — fail closed."""


def _content_hash(text: str, context: str) -> str:
    """sha256 over text + a NUL-bearing separator + context. Drift → re-scrub."""
    payload = (text or "") + "\n\x00" + (context or "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _assert_ner_available():
    """Return the verified DeBERTa NER pipeline, or raise
    :class:`NerUnavailable` unless it is loaded AND its entity pass runs
    (verified against a probe). This is the fail-closed gate — it deliberately
    calls ``get_pii_pipeline`` directly instead of going through the redactor,
    whose swallow would hide the degradation. The returned pipe is reused by
    :func:`_assert_output_clean` so both belts verify the same instance."""
    from apps.pii.config import DEBERTA_LABEL_MAP
    from apps.pii.engine import get_pii_pipeline

    try:
        pipe = get_pii_pipeline()  # re-raises the cached load error when unavailable
    except Exception as exc:  # noqa: BLE001 — any load failure is fail-closed
        raise NerUnavailable(f"DeBERTa NER pipeline unavailable: {exc}") from exc
    if pipe is None:
        raise NerUnavailable("DeBERTa NER pipeline is None")
    try:
        detections = pipe(_NER_PROBE)
    except Exception as exc:  # noqa: BLE001 — inference failure is fail-closed
        raise NerUnavailable(f"DeBERTa NER inference failed: {exc}") from exc
    labels = {DEBERTA_LABEL_MAP.get(d.get("entity_group") or "") for d in (detections or [])}
    if "PERSON" not in labels:
        raise NerUnavailable(
            "DeBERTa NER pass did not detect the probe PERSON — refusing to fall back to Presidio-only"
        )
    return pipe


# Labels that must NEVER survive into a published snapshot, enforced by the
# second belt below. LOCATION is deliberately excluded: the raw pipeline (no
# redactor score-tier logic) false-fires on number-ish tokens (see the
# BUILDINGNUMBER history in apps/pii/config.py), and a surviving place name is
# lower-stakes than a name/contact. PERSON/EMAIL/PHONE at >=0.7 are unambiguous.
_OUTPUT_FORBIDDEN_LABELS = frozenset({"PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"})
_OUTPUT_SCORE_FLOOR = 0.7


def _identity_allow_names(owner_tenant) -> set[str]:
    """The owner's own display name (full + first/last parts), lowercased —
    mirrors ``_redact``'s allow-list construction. A share is overtly
    attributed to the owner (it publishes as them, name attached), so their own
    name inside the text adds zero identity information and the redaction pass
    deliberately leaves it raw."""
    user = getattr(owner_tenant, "user", None)
    display_name = (getattr(user, "display_name", "") or "").strip()
    if not display_name:
        return set()
    names = {display_name}
    parts = display_name.split()
    if len(parts) > 1:
        names |= {parts[0], parts[-1]}
    return {n.lower() for n in names}


def _deliberately_unredacted(span: str, label: str, owner_tenant, allow_lower: set[str]) -> bool:
    """True when the redaction pass would have DELIBERATELY left ``span`` raw —
    i.e. its presence in the output is policy, not a degradation.

    Mirrors the identity-label skips in ``apps.pii.redactor._filter_results``:
    the owner's own name (PERSON only — overt in an attributed share), the
    tenant's denylist ("not PII for me", type-agnostic by design), and the
    degenerate-span floor (< 3 chars is never real PII, the redactor's own
    rule). WITHOUT this, any lesson containing one of those spans fail-closes
    on every attempt and is unshareable by design contradiction — the prod
    lesson-899 case: DeBERTa flagged sub-word fragments of a denylisted brand
    plus the owner's own initials as PERSON in the *correctly* scrubbed output.
    Anything not excused here still fail-closes, so a REAL leaked name (which
    ``_filter_results`` would have redacted, not skipped) is caught exactly as
    before."""
    from apps.pii.entity_registry import is_denied
    from apps.pii.redactor import _is_degenerate_span

    stripped = (span or "").strip()
    if not stripped:
        return True
    if label == "PERSON" and stripped.lower() in allow_lower:
        return True
    if is_denied(getattr(owner_tenant, "pii_denylist", None) or {}, stripped):
        return True
    if _is_degenerate_span(stripped):
        return True
    return False


def _assert_output_clean(pipe, outputs, owner_tenant=None) -> None:
    """The SECOND belt (design §4.3): ``RedactionSession.redact()`` swallows
    per-call inference errors and returns near-raw text, so a failure AFTER the
    probe passed could otherwise publish real names as "scrubbed". The
    probe-verified NER pass must find no high-confidence identity entity in the
    text we are about to freeze — a hit means some upstream step silently
    degraded on THIS text. Fail closed; the owner can edit and retry.

    Precision (parity with the redaction pass, never more permissive on real
    names): PERSON spans are snapped to word boundaries first — the raw
    pipeline reports sub-word fragments ("aut" inside a brand name) that the
    redactor's own detection path snaps before judging — and a hit is excused
    ONLY when the redaction pass deliberately leaves that exact span raw
    (owner's own name / tenant denylist / degenerate floor; see
    :func:`_deliberately_unredacted`). ``owner_tenant=None`` applies no
    owner-specific excuse — strictly MORE fail-closed, never less."""
    from apps.pii.config import DEBERTA_LABEL_MAP

    try:
        from apps.pii.hygiene import snap_to_word_boundaries
    except Exception:  # pragma: no cover — hygiene absent → judge raw spans (stricter)
        snap_to_word_boundaries = None

    allow_lower = _identity_allow_names(owner_tenant) if owner_tenant is not None else set()

    for out in outputs:
        if not out:
            continue
        try:
            detections = pipe(out)
        except Exception as exc:  # noqa: BLE001 — verification failure is fail-closed
            raise NerUnavailable(f"output verification inference failed: {exc}") from exc
        hits = set()
        for d in detections or []:
            label = DEBERTA_LABEL_MAP.get(d.get("entity_group") or "")
            if label not in _OUTPUT_FORBIDDEN_LABELS:
                continue
            if float(d.get("score") or 0.0) < _OUTPUT_SCORE_FLOOR:
                continue
            start, end = int(d.get("start") or 0), int(d.get("end") or 0)
            if label == "PERSON" and snap_to_word_boundaries is not None:
                # Expansion-only (mirrors _detect_pii): recover the whole word so
                # we judge "Sautai", never the fragment "aut". It cannot cross
                # whitespace, so it can never excuse more than the touched word.
                start, end = snap_to_word_boundaries(out, start, end)
            span = out[start:end]
            if _deliberately_unredacted(span, label, owner_tenant, allow_lower):
                continue
            hits.add(label)
        if hits:
            raise NerUnavailable(
                f"scrubbed output still contains identity entities ({', '.join(sorted(hits))}) — refusing to publish"
            )


def _redact_identities(owner_tenant, text: str) -> str:
    """Redact identity PII with the OWNER's session (their pii_entity_map +
    denylist seed). Kept a separate seam so tests can patch it without loading
    the model."""
    from apps.pii.redactor import RedactionSession

    if not text:
        return ""
    return RedactionSession(tenant=owner_tenant).redact(text)


def _neutralize(owner_tenant, text: str) -> str:
    """Redact identities, then replace EVERY residual ``[TYPE_N]`` placeholder
    with a generic word — no map persisted, so it cannot be un-scrubbed."""
    from apps.lessons.copilot import _scrub_placeholders

    return _scrub_placeholders(_redact_identities(owner_tenant, text))


def _allowlist_tags(tags) -> list[str]:
    out: list[str] = []
    for tag in tags or []:
        slug = str(tag).strip().lower()
        if _SAFE_TAG_RE.match(slug):
            out.append(slug)
    return out[:12]


def scrub_shared_lesson(shared_lesson_id, pending_share_id: str | None = None) -> dict:
    """Scrub one ``SharedLesson`` fail-closed. Idempotent: a re-run over
    unchanged content is a no-op; a content_hash drift (owner edited the source,
    or the human supplied an edited ``final_text``) re-scrubs.

    Returns a small status dict (never leaks content).
    """
    from . import access
    from .models import PendingShare, SharedLesson

    shared_lesson = access.get_shared_lesson(shared_lesson_id)
    if shared_lesson is None:
        return {"ok": False, "reason": "shared_lesson_not_found"}

    lesson = shared_lesson.source_lesson  # owner's own lesson via FK — never Lesson.objects
    owner = shared_lesson.owner_tenant

    # An edit (human-supplied final_text on the pending share) re-scrubs the
    # edited text; otherwise the source lesson's text.
    text = lesson.text or ""
    if pending_share_id:
        pending = PendingShare.objects.filter(id=pending_share_id).first()
        if pending and pending.final_text:
            text = pending.final_text
    context = lesson.context or ""
    content_hash = _content_hash(text, context)

    if shared_lesson.scrub_status == SharedLesson.ScrubStatus.READY and shared_lesson.content_hash == content_hash:
        return {"ok": True, "reason": "already_ready"}

    # ── FAIL-CLOSED gate (belt 1: the pipeline itself works) ──
    try:
        pipe = _assert_ner_available()
    except Exception as exc:  # noqa: BLE001 — verified-or-fail-closed
        access.save_scrub_failed(shared_lesson, f"NER path unavailable — refusing to share (fail-closed): {exc}")
        logger.warning("share scrub fail-closed for %s: NER unavailable", shared_lesson_id)
        return {"ok": False, "reason": "ner_unavailable"}

    try:
        redacted_text = _neutralize(owner, text)
        redacted_context = _neutralize(owner, context)
        redacted_cluster = _neutralize(owner, lesson.cluster_label or "")
    except Exception as exc:  # noqa: BLE001 — any redaction error is fail-closed
        access.save_scrub_failed(shared_lesson, f"scrub error: {exc}")
        return {"ok": False, "reason": "scrub_error"}

    # ── FAIL-CLOSED gate (belt 2: THIS text actually got cleaned) ──
    # RedactionSession.redact() swallows per-call inference errors and returns
    # near-raw text; without this check such a failure would publish real names.
    # ``owner`` carries the deliberate-skip semantics (own name / denylist) so
    # the belt judges the output by the same policy the redaction pass applied.
    try:
        _assert_output_clean(pipe, [redacted_text, redacted_context, redacted_cluster], owner_tenant=owner)
    except Exception as exc:  # noqa: BLE001 — verification failure is fail-closed
        access.save_scrub_failed(shared_lesson, f"output verification failed (fail-closed): {exc}")
        logger.warning("share scrub fail-closed for %s: output not clean", shared_lesson_id)
        return {"ok": False, "reason": "output_not_clean"}

    access.save_scrub_ready(
        shared_lesson,
        redacted_text=redacted_text,
        redacted_context=redacted_context,
        tags=_allowlist_tags(lesson.tags),
        cluster_label=redacted_cluster[:200],
        position_x=lesson.position_x,
        position_y=lesson.position_y,
        star_stage=lesson.star_stage or "proto",
        content_hash=content_hash,
        scrub_model_version=SCRUB_MODEL_VERSION,
    )
    return {"ok": True, "reason": "ready"}
