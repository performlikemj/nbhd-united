"""Swift-parity lookup for the illustrated Workout Guide catalog.

This normalizer is intentionally separate from
``apps.common.llm_lookups.normalize_exercise`` (a longest-substring lookup used
for category/metric inference on every write) and
``apps.pii.redactor._span_tokens`` (PII span tokenization). This third form
exists solely to match the iOS Swift picture contract exactly. None of these
normalizers rewrites an exercise name.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_CATALOG_PATH = Path(__file__).with_name("data") / "workout_guide_catalog.json"
_TOKEN_RE = re.compile(r"[a-z]+")


@dataclass(frozen=True, slots=True)
class Entry:
    slug: str
    name: str
    equipment: str
    primaryMuscle: str
    isStretch: bool
    frames: int

    def image_name(self, frame: int) -> str:
        """Return the iOS asset name for ``frame`` (kept for parity tests)."""
        return f"wg-{self.slug}-{frame}"


def normalize(name: str) -> str:
    """Return the exact ASCII-alphanumeric key used by the iOS catalog."""
    out: list[str] = []
    pending_dash = False
    for char in str(name).lower():
        if char in {"'", "’"}:
            continue
        if char.isascii() and char.isalnum():
            if pending_dash and out:
                out.append("-")
            pending_dash = False
            out.append(char)
        else:
            pending_dash = True
    return "".join(out)


def _facet_key(value: str) -> str:
    value = value.strip().casefold()
    return value[:-1] if value.endswith("s") else value


class Catalog:
    """Immutable in-memory indexes built from one catalog document."""

    def __init__(self, entries: list[Entry], aliases: dict[str, str], *, metadata: dict[str, Any] | None = None):
        self.entries = tuple(entries)
        self.aliases = dict(aliases)
        self.metadata = dict(metadata or {})
        by_slug = {entry.slug: entry for entry in self.entries}
        index: dict[str, Entry] = {}
        alias_keys_by_slug: dict[str, list[str]] = {}
        for entry in self.entries:
            index[entry.slug] = entry
            index[normalize(entry.name)] = entry
        for alias, slug in self.aliases.items():
            entry = by_slug.get(slug)
            if entry is None:
                continue
            key = normalize(alias)
            index.setdefault(key, entry)
            alias_keys_by_slug.setdefault(slug, []).append(key)
        self._index = index
        self._alias_keys_by_slug = {slug: tuple(keys) for slug, keys in alias_keys_by_slug.items()}

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> Catalog:
        entries = [Entry(**raw) for raw in document.get("entries", [])]
        metadata = {key: value for key, value in document.items() if key not in {"entries", "aliases"}}
        return cls(entries, document.get("aliases", {}), metadata=metadata)

    def match(self, name: str) -> Entry | None:
        key = normalize(name)
        if not key:
            return None
        hit = self._index.get(key)
        if hit is not None:
            return hit
        if key.endswith("s"):
            hit = self._index.get(key[:-1])
            if hit is not None:
                return hit
        if key.endswith("es"):
            return self._index.get(key[:-2])
        return None

    def search(
        self,
        query: str | None = None,
        *,
        muscle: str | None = None,
        equipment: str | None = None,
        limit: int = 20,
    ) -> list[Entry]:
        q = normalize(query or "")
        exact = self.match(query or "") if q else None
        muscle_key = _facet_key(muscle) if muscle else None
        equipment_key = _facet_key(equipment) if equipment else None
        hits: list[Entry] = []
        for entry in self.entries:
            if muscle_key and _facet_key(entry.primaryMuscle) != muscle_key:
                continue
            if equipment_key and _facet_key(entry.equipment) != equipment_key:
                continue
            if q:
                aliases = self._alias_keys_by_slug.get(entry.slug, ())
                if not (
                    entry == exact
                    or q in entry.slug
                    or q in normalize(entry.name)
                    or q in normalize(entry.primaryMuscle)
                    or any(q in alias for alias in aliases)
                ):
                    continue
            hits.append(entry)
        # Swift is authoritative: exact first, then localized/name order.
        hits.sort(key=lambda entry: (entry != exact, entry.name.casefold()))
        return hits[: max(0, int(limit))]

    def muscles(self) -> list[str]:
        return sorted({entry.primaryMuscle for entry in self.entries})

    def equipment_types(self) -> list[str]:
        return sorted({entry.equipment for entry in self.entries})

    def vocabulary(self) -> frozenset[str]:
        tokens: set[str] = set()
        for entry in self.entries:
            tokens.update(_TOKEN_RE.findall(entry.name.casefold()))
        for alias in self.aliases:
            tokens.update(_TOKEN_RE.findall(alias.casefold()))
        return frozenset(tokens)


@lru_cache(maxsize=1)
def _catalog() -> Catalog:
    with _CATALOG_PATH.open(encoding="utf-8") as handle:
        return Catalog.from_document(json.load(handle))


def match(name: str) -> Entry | None:
    return _catalog().match(name)


def search(
    query: str | None = None,
    muscle: str | None = None,
    equipment: str | None = None,
    limit: int = 20,
) -> list[Entry]:
    return _catalog().search(query, muscle=muscle, equipment=equipment, limit=limit)


def muscles() -> list[str]:
    return _catalog().muscles()


def equipment_types() -> list[str]:
    return _catalog().equipment_types()


def vocabulary() -> frozenset[str]:
    return _catalog().vocabulary()
