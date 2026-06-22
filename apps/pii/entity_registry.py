"""Helpers for reading and writing the per-tenant entity registry.

The registry lives on ``Tenant.pii_entity_map`` (a JSONField) and maps
PII placeholders like ``[PERSON_1]`` to entity metadata. It started life
as ``Dict[str, str]`` (placeholder → original name); this module extends
it to ``Dict[str, Dict[str, Any]]`` with ``name`` + optional
``relationship`` + ``notes`` + ``updated_at`` while keeping reads of the
legacy string-shaped entries working.

Why a registry, not just a redaction dict
-----------------------------------------

PR #649 added a USER.md envelope section that tells the agent to preserve
``[PERSON_X]`` placeholders verbatim. That deliberately denies the agent
any identity context for those placeholders, on the grounds that the
hallucination risk of restoring real names outweighs the benefit. The
trade-off is real: the agent can't disambiguate "she" or "they" because
it has no metadata about who ``[PERSON_1]`` is.

Storing ``relationship`` and ``notes`` per entry lets a follow-up PR
(Issue 2b) inject *user-curated* identity context into the prompt
without re-introducing the hallucination risk — the agent sees
"``[PERSON_1]`` is the user's daughter, age 4.5" instead of either the
real name or nothing.

Backward compatibility
----------------------

Existing in-prod data is ``{"[PERSON_1]": "Nana"}``. After this module
ships, reads coerce string entries to ``{"name": "Nana"}`` on the fly.
Writes always use the new dict shape going forward, so the map migrates
opportunistically as redaction fires. No explicit migration is needed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

# Keys allowed in an entity entry. Anything else is dropped on normalize.
# ``arbiter_judged_at`` is an internal stamp written by the PII arbiter cron
# (apps/pii/arbiter.py) so already-judged entries skip re-evaluation; it
# stays out of ``get_metadata`` because it isn't user-facing identity context.
_KNOWN_FIELDS = ("name", "relationship", "notes", "updated_at", "arbiter_judged_at")

_PLACEHOLDER_NUM_RE = re.compile(r"\[[A-Z_]+_(\d+)\]")


def coerce(entry: Any) -> dict[str, Any]:
    """Normalize an entry from either the legacy string shape or the
    new dict shape into a canonical dict with at least ``name``.

    - ``"Nana"`` → ``{"name": "Nana"}``
    - ``{"name": "Nana", "relationship": "daughter"}`` → unchanged
    - ``{"name": "Nana", "junk": "..."}`` → ``{"name": "Nana"}``
      (unknown fields dropped)
    - ``None`` / ``""`` / non-string non-dict → ``{"name": ""}``

    Always returns a fresh dict; safe to mutate by the caller.
    """
    if isinstance(entry, dict):
        out: dict[str, Any] = {}
        for k in _KNOWN_FIELDS:
            v = entry.get(k)
            if v is not None:
                out[k] = v
        out.setdefault("name", "")
        return out
    if isinstance(entry, str):
        return {"name": entry}
    return {"name": ""}


def get_name(entry: Any) -> str:
    """Return the original/canonical name for an entry, across both
    legacy string entries and new dict entries. Empty string when
    unset or malformed.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        name = entry.get("name")
        return name if isinstance(name, str) else ""
    return ""


def get_metadata(entry: Any) -> dict[str, Any]:
    """Return the identity metadata (relationship / notes / updated_at)
    for an entry, never including ``name``. Empty dict for legacy
    string entries — they carry no metadata.
    """
    if not isinstance(entry, dict):
        return {}
    return {k: entry[k] for k in ("relationship", "notes", "updated_at") if entry.get(k)}


def to_storage_value(
    name: str,
    *,
    relationship: str = "",
    notes: str = "",
    updated_at: str | None = None,
    arbiter_judged_at: str | None = None,
) -> dict[str, Any]:
    """Build a canonical dict entry for writing back to
    ``Tenant.pii_entity_map``. Empty optional fields are omitted so
    the JSON stays compact.

    ``arbiter_judged_at`` is the internal stamp written by the PII arbiter
    cron to record that an entry has already been evaluated, preventing
    redundant re-evaluation on the next sweep. Pass the existing stamp when
    updating a user-editable entry so it is preserved across PATCH round-trips.
    """
    out: dict[str, Any] = {"name": name}
    if relationship:
        out["relationship"] = relationship
    if notes:
        out["notes"] = notes
    if updated_at:
        out["updated_at"] = updated_at
    if arbiter_judged_at:
        out["arbiter_judged_at"] = arbiter_judged_at
    return out


def iter_normalized(entity_map: dict[str, Any] | None) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(placeholder, coerced_entry)`` pairs for every entry in
    the map, coercing legacy string entries to dict shape on the fly.
    """
    if not entity_map:
        return
    for placeholder, entry in entity_map.items():
        yield placeholder, coerce(entry)


def inverted_names(entity_map: dict[str, Any] | None) -> dict[str, str]:
    """Return a ``name -> placeholder`` mapping derived from the entity
    map. Used by the redactor's known-entity pass so previously seen
    names re-collide to their existing placeholder regardless of
    storage shape. Entries with empty names are skipped.
    """
    out: dict[str, str] = {}
    if not entity_map:
        return out
    for placeholder, entry in entity_map.items():
        name = get_name(entry)
        if name:
            out[name] = placeholder
    return out


def canonical_key(name: str) -> str:
    """Return the case-insensitive canonical form of a name for entity
    merging. Casefolded + stripped of leading/trailing whitespace.

    Why casefold and not lower: casefold handles "ß"/"ss" and Turkish
    dotted/dotless I, which lower() does not. Same cost, more correct
    for the multilingual tenant base.

    Empty / non-string inputs return ``""`` — callers should treat that
    as "no key, skip this entry."
    """
    if not isinstance(name, str):
        return ""
    return name.casefold().strip()


def _placeholder_num(placeholder: str) -> int:
    """Parse the numeric suffix from ``[ETYPE_N]``. Returns 0 for
    malformed placeholders so they sort first and lose canonical-pick
    ties to well-formed neighbours.
    """
    match = _PLACEHOLDER_NUM_RE.match(placeholder)
    return int(match.group(1)) if match else 0


def inverted_names_ci(
    entity_map: dict[str, Any] | None,
) -> dict[str, tuple[str, str]]:
    """Case-insensitive variant of ``inverted_names``.

    Returns ``canonical_key -> (display_name, placeholder)`` so that
    "Sautai", "sautai", and " Sautai " all resolve to the same
    placeholder. When multiple entries share a canonical key (legacy
    bloat from the bug this exists to fix), the lowest-numbered
    placeholder wins — that's the canonical mint, and rehydration
    keeps working for the duplicates because the underlying map is
    unchanged.

    Used by the redactor's known-entity pass + post-NER hit check.
    """
    out: dict[str, tuple[str, str]] = {}
    if not entity_map:
        return out
    for placeholder, entry in entity_map.items():
        name = get_name(entry)
        key = canonical_key(name)
        if not key:
            continue
        # Strip outer whitespace from the display form so callers can
        # feed it straight into ``re.escape`` and match unpadded user
        # text. The underlying entity_map keeps its padded value for
        # rehydration; that path keys by placeholder, not by name.
        display = name.strip()
        existing = out.get(key)
        if existing is None or _placeholder_num(placeholder) < _placeholder_num(existing[1]):
            out[key] = (display, placeholder)
    return out


def is_denied(denylist: dict[str, Any] | None, name: str) -> bool:
    """True when ``name`` is on the tenant's PII denylist.

    The denylist is a per-tenant ``Dict[canonical_key, metadata_dict]``.
    Lookup is by ``canonical_key(name)`` so casing / whitespace variants
    of the same denied word all match. The metadata is unused by this
    check — it carries provenance (reason, decided_at) for downstream
    consumers like the admin UI.

    Empty / None denylist returns False, preserving the today's-behavior
    semantics for tenants who haven't added anything.
    """
    if not denylist:
        return False
    key = canonical_key(name)
    if not key:
        return False
    return key in denylist


def normalize_denylist_key(name: str) -> str:
    """Public alias for ``canonical_key`` to make denylist write call
    sites self-documenting. Use when adding to a tenant's pii_denylist
    so the key shape stays consistent fleet-wide.
    """
    return canonical_key(name)
