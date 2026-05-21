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

from collections.abc import Iterator
from typing import Any

# Keys allowed in an entity entry. Anything else is dropped on normalize.
_KNOWN_FIELDS = ("name", "relationship", "notes", "updated_at")


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
) -> dict[str, Any]:
    """Build a canonical dict entry for writing back to
    ``Tenant.pii_entity_map``. Empty optional fields are omitted so
    the JSON stays compact.
    """
    out: dict[str, Any] = {"name": name}
    if relationship:
        out["relationship"] = relationship
    if notes:
        out["notes"] = notes
    if updated_at:
        out["updated_at"] = updated_at
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
