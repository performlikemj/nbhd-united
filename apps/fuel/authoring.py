"""Fuel authoring glue: validated set actuals are machine data, not prose."""

from copy import deepcopy

from pydantic import ValidationError

from .set_contract import _validate_logged


def logged_actuals_paths(value, path=()):
    """Snapshot only valid actuals at exercise/skill set paths, including plans."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("exercises", "skills") and isinstance(child, list):
                for i, ex in enumerate(child):
                    if not isinstance(ex, dict) or not isinstance(ex.get("sets"), list):
                        continue
                    for j, s in enumerate(ex["sets"]):
                        if not isinstance(s, dict) or "logged" not in s:
                            continue
                        try:
                            _validate_logged(s, str(ex.get("name") or ""))
                        except ValidationError:
                            continue
                        found.append(((*path, key, i, "sets", j, "logged"), deepcopy(s["logged"])))
            else:
                found.extend(logged_actuals_paths(child, (*path, key)))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            found.extend(logged_actuals_paths(child, (*path, i)))
    return found


def restore_logged_actuals(value, snapshots):
    """Restore snapshots after text transforms without changing prose or shape."""
    if not snapshots:
        return value
    result = deepcopy(value)
    for path, logged in snapshots:
        current = result
        try:
            for part in path[:-1]:
                current = current[part]
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(current, dict):
            current[path[-1]] = logged
    return result


def author_store_fields(tenant, data, **kwargs):
    """Use the shared PII author, then retain validated numeric/UTC actuals.

    Like catalog identities, machine actuals must survive known-value text
    substitution. Freeform text and invalid actuals receive no exemption.
    """
    from apps.pii.store_authoring import author_store_fields as author

    snapshots = logged_actuals_paths(data)
    authored, receipts = author(tenant, data, **kwargs)
    return restore_logged_actuals(authored, snapshots), receipts
