"""Vocabulary hinting for server-side voice transcription.

OpenAI Whisper transcribes a short clip with no knowledge of the speaker's
world, so distinctive proper nouns — brands, project names, people — come back
*phonetically approximated*. A Japanese brand like "Rakuten" (楽天) is decoded
as "Rocketen". That misheard token is then written verbatim into the tenant's
journal, and because the PII layer mirrors journal text to the file share
(``apps/orchestrator/memory_sync.py``) the NER model mislabels it as a PERSON
and freezes it into ``Tenant.pii_entity_map`` — from where the daily-summary
carry-forward reproduces it in every later message about the user's goals.

Whisper accepts an optional ``prompt`` that biases decoding toward the
vocabulary it contains (it is decoder context, not an instruction).
``build_transcription_prompt`` assembles that hint from the tenant's OWN
already-known, non-PII vocabulary so a name the user has used before is spelled
consistently instead of re-guessed on every clip.

PII boundary
------------
The audio already goes to OpenAI, so this adds no new *audio* egress. For the
text hint we deliberately draw ONLY from sources the user or the PII arbiter
have already declared non-identifying:

* ``pii_denylist`` keys — brands / projects / jargon explicitly marked
  "not PII for me" (this is exactly where "rakuten" lands once denylisted, so
  the same signal that stops the redaction confusion also fixes the spelling).
* workspace names — user-authored labels.
* the user's own display name.

Contact names living in ``pii_entity_map`` are intentionally excluded: those
are third-party PERSON entities the module works to keep out of provider
prompts, and the transcription win does not justify widening their egress.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Whisper caps the prompt at ~224 tokens; stay well under with coarse budgets.
_MAX_TERMS = 48
_MAX_PROMPT_CHARS = 600


def build_transcription_prompt(tenant: Tenant | None) -> str | None:
    """Return a Whisper ``prompt`` biasing decoding toward the tenant's known
    non-PII proper nouns, or ``None`` when there is no useful vocabulary.

    Never raises: any lookup failure degrades to ``None`` so transcription
    proceeds exactly as it does today.
    """
    if tenant is None:
        return None
    try:
        terms = _collect_vocabulary(tenant)
    except Exception:
        logger.debug("build_transcription_prompt: vocabulary lookup failed", exc_info=True)
        return None
    if not terms:
        return None
    # A short natural-language frame reads better to Whisper than a bare list
    # and keeps the terms in "these words may appear" decoder context.
    return "The speaker may mention: " + ", ".join(terms) + "."


def _collect_vocabulary(tenant: Tenant) -> list[str]:
    """Ordered, de-duplicated vocabulary terms, highest-signal first.

    Order: denylisted brands / jargon, then workspace names, then the user's
    own name — capped by term count and total length to respect Whisper's
    prompt budget.
    """
    seen: set[str] = set()
    terms: list[str] = []

    def _add(raw: object) -> None:
        if not isinstance(raw, str):
            return
        term = raw.strip()
        # Single characters carry no spelling signal.
        if len(term) < 2:
            return
        key = term.casefold()
        if key in seen:
            return
        seen.add(key)
        terms.append(term)

    # 1. Denylist keys: the user/arbiter-vetted "not PII" vocabulary. Keys are
    #    canonical (casefolded); title-case bare single tokens so Whisper is
    #    biased toward the natural proper-noun spelling ("rakuten" -> "Rakuten").
    denylist = getattr(tenant, "pii_denylist", None) or {}
    if isinstance(denylist, dict):
        for key in denylist:
            _add(_titleish(key))

    # 2. Workspace names — user-authored labels (e.g. project names).
    for name in _workspace_names(tenant):
        _add(name)

    # 3. The user's own display name.
    user = getattr(tenant, "user", None)
    _add(getattr(user, "display_name", "") if user is not None else "")

    # Enforce the prompt budget.
    out: list[str] = []
    total = 0
    for term in terms:
        if len(out) >= _MAX_TERMS:
            break
        total += len(term) + 2
        if total > _MAX_PROMPT_CHARS:
            break
        out.append(term)
    return out


def _titleish(key: str) -> str:
    """Title-case a bare lowercase token so it reads as a proper noun; leave
    multi-word or already-cased strings untouched.
    """
    if key and key.islower() and " " not in key:
        return key[:1].upper() + key[1:]
    return key


def _workspace_names(tenant: Tenant) -> list[str]:
    """Best-effort list of the tenant's workspace names (empty on any error)."""
    try:
        return [n for n in tenant.workspaces.values_list("name", flat=True)[:_MAX_TERMS] if n]
    except Exception:
        return []
