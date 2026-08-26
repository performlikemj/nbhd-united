"""Ingress-only catalog identity annotation for runtime fuel writes."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from . import catalog


@dataclass(frozen=True, slots=True)
class IncomingPath:
    """A normalized payload path paired with its caller-visible location."""

    path: tuple[str | int, ...]
    loc: tuple[str | int, ...]


def incoming_name_paths(value: Any, prefix: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    """Return exercise/skill name leaves present in ``value`` in request order."""
    paths: list[tuple[str | int, ...]] = []

    def walk(node: Any, path: tuple[str | int, ...]) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = (*path, key)
                if key in {"exercises", "skills"} and isinstance(child, list):
                    for index, item in enumerate(child):
                        if isinstance(item, dict) and isinstance(item.get("name"), str):
                            paths.append((*child_path, index, "name"))
                walk(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, (*path, index))

    walk(value, prefix)
    return paths


def _get_path(payload: Any, path: tuple[str | int, ...]) -> Any:
    current = payload
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                return None
        elif not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_path(payload: Any, path: tuple[str | int, ...], value: Any) -> bool:
    if not path:
        return False
    parent = _get_path(payload, path[:-1])
    leaf = path[-1]
    if isinstance(parent, dict) and isinstance(leaf, str):
        parent[leaf] = value
        return True
    if isinstance(parent, list) and isinstance(leaf, int) and leaf < len(parent):
        parent[leaf] = value
        return True
    return False


def annotate_incoming(
    payload: Any,
    incoming_paths: Iterable[IncomingPath | tuple[str | int, ...]],
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """Annotate only caller-supplied name leaves, returning safe match metadata."""
    annotated = deepcopy(payload)
    matches: list[dict[str, Any]] = []
    unmatched: list[str] = []
    seen_unmatched: set[str] = set()
    version = catalog._catalog().metadata.get("version")

    for supplied in incoming_paths:
        spec = supplied if isinstance(supplied, IncomingPath) else IncomingPath(tuple(supplied), tuple(supplied))
        name = _get_path(annotated, spec.path)
        if not isinstance(name, str) or not name.strip():
            continue
        item = _get_path(annotated, spec.path[:-1])
        if not isinstance(item, dict):
            continue

        resolution = catalog.resolve_name(name)
        if resolution is None:
            item.pop("catalog_ref", None)
            if item.get("user_verbatim") is not True and name not in seen_unmatched:
                seen_unmatched.add(name)
                unmatched.append(name)
            continue

        existing = item.get("catalog_ref")
        if isinstance(existing, dict) and existing.get("slug") == resolution.entry.slug:
            catalog_ref = deepcopy(existing)
        else:
            catalog_ref = {
                "slug": resolution.entry.slug,
                "version": version,
                "matched_by": resolution.matched_by,
            }
        item["catalog_ref"] = catalog_ref
        matches.append(
            {
                "loc": list(spec.loc),
                "slug": resolution.entry.slug,
                "matched_by": str(catalog_ref.get("matched_by") or resolution.matched_by),
                "name": resolution.entry.name,
            }
        )

    return annotated, matches, unmatched


def reinsert_catalog_refs(authored: Any, server_owned: Any) -> Any:
    """Restore every pre-authoring catalog_ref without resolving any new path."""
    restored = deepcopy(authored)

    def walk(source: Any, target: Any) -> None:
        if isinstance(source, dict) and isinstance(target, dict):
            ref = source.get("catalog_ref")
            if isinstance(ref, dict):
                target["catalog_ref"] = deepcopy(ref)
            for key, child in source.items():
                if key == "catalog_ref" or key not in target:
                    continue
                walk(child, target[key])
        elif isinstance(source, list) and isinstance(target, list):
            for source_child, target_child in zip(source, target, strict=False):
                walk(source_child, target_child)

    walk(server_owned, restored)
    return restored
