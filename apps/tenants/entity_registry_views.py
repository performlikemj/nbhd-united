"""Paged registry reads and reversible, owner-directed bulk lifecycle changes."""

from django.core import signing
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.pii.entity_registry import canonical_key, coerce, get_name, is_retired, to_storage_value

from .models import Tenant

REGISTRY_QUERY_PARAMS = {"q", "type", "state", "everyday", "page_size", "cursor", "select_all"}
MAX_BATCH = 2000
_CURSOR_SALT = "entity-registry-page-v1"


def looks_like_everyday_word(name):
    """Advisory only: share existing rules without changing redaction policy."""
    from apps.pii.hygiene import is_junk_span
    from apps.pii.redactor import is_never_a_name

    token = name.strip()
    return is_never_a_name(name) or (
        token.isalpha() and token.lower() != token.upper() and is_junk_span(token.lower(), "PERSON")[0]
    )


def paged_registry_response(tenant, params):
    state = params.get("state", "active")
    if state not in {"active", "stopped"}:
        raise ValidationError({"detail": "state must be active or stopped"})
    for key in ("everyday", "select_all"):
        if key in params and params[key] not in {"true", "false"}:
            raise ValidationError({"detail": f"{key} must be true or false"})
    try:
        page_size = min(int(params.get("page_size", 100)), 200)
        if page_size < 1:
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError({"detail": "page_size must be a positive integer"}) from None

    query = params.get("q", "").casefold()
    entity_type = params.get("type", "")
    everyday = params.get("everyday") == "true"
    scope = [str(tenant.pk), query, entity_type, state, everyday]
    after = None
    if "cursor" in params:
        try:
            cursor = signing.loads(params["cursor"], salt=_CURSOR_SALT)
            if not isinstance(cursor, dict) or cursor.get("scope") != scope:
                raise ValueError
            after = cursor["after"]
            if not isinstance(after, list) or len(after) != 2 or not all(isinstance(v, str) for v in after):
                raise ValueError
            after = tuple(after)
        except (signing.BadSignature, ValueError, TypeError, KeyError):
            raise ValidationError({"detail": "Invalid cursor for these filters"}) from None

    counts = {"active": 0, "stopped": 0}
    entries = []
    for placeholder, raw in (tenant.pii_entity_map or {}).items():
        entry_state = "stopped" if is_retired(raw) else "active"
        counts[entry_state] += 1
        prefix = placeholder.strip("[]").rsplit("_", 1)[0]
        if entry_state != state or (entity_type and entity_type != prefix):
            continue
        entry = coerce(raw)
        name = get_name(raw)
        if query and not any(
            query in str(value).casefold()
            for value in (name, entry.get("relationship", ""), entry.get("notes", ""), placeholder)
        ):
            continue
        looks_everyday = looks_like_everyday_word(name)
        if everyday and not looks_everyday:
            continue
        metadata = raw if isinstance(raw, dict) else {}
        entries.append(
            {
                "placeholder": placeholder,
                "name": name,
                "relationship": entry.get("relationship", ""),
                "notes": entry.get("notes", ""),
                "updated_at": entry.get("updated_at"),
                "entity_type": prefix,
                "state": entry_state,
                "persistence": "provisional" if metadata.get("provisional") else "permanent",
                "reviewed": bool(metadata.get("reviewed_at")),
                "looks_like_everyday_word": looks_everyday,
            }
        )

    def sort_key(entry):
        return entry["name"].casefold(), entry["placeholder"]

    entries.sort(key=sort_key)
    total = len(entries)
    if params.get("select_all") == "true":
        if total > MAX_BATCH:
            return Response({"detail": "too_many", "total": total}, status=422)
        return Response({"placeholders": [entry["placeholder"] for entry in entries], "total": total})
    if after is not None:
        entries = [entry for entry in entries if sort_key(entry) > after]
    page = entries[:page_size]
    next_cursor = None
    if len(entries) > page_size:
        next_cursor = signing.dumps({"scope": scope, "after": sort_key(page[-1])}, salt=_CURSOR_SALT)
    return Response({"entries": page, "total": total, "next_cursor": next_cursor, "counts": counts})


def _placeholders(data):
    placeholders = data.get("placeholders") if isinstance(data, dict) else None
    if not isinstance(placeholders, list) or not 1 <= len(placeholders) <= MAX_BATCH:
        raise ValidationError({"detail": f"placeholders must be a list of 1..{MAX_BATCH} strings"})
    if not all(isinstance(value, str) and value.strip() for value in placeholders):
        raise ValidationError({"detail": "placeholders must contain non-empty strings"})
    return placeholders


class EntityRegistryBulkStopView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        placeholders = _placeholders(request.data)
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=404)

        with transaction.atomic():
            locked = Tenant.objects.select_for_update().get(pk=tenant.pk)
            entity_map = dict(locked.pii_entity_map or {})
            denylist = dict(locked.pii_denylist or {})
            now = timezone.now().isoformat()
            results = []
            names = {}
            for placeholder in placeholders:
                if placeholder not in entity_map:
                    results.append({"placeholder": placeholder, "status": "not_found"})
                    continue
                key = canonical_key(get_name(entity_map[placeholder]))
                result = {"placeholder": placeholder, "key": key, "status": "already_stopped"}
                results.append(result)
                if key in names:
                    result["status"] = "duplicate"
                    continue
                names[key] = result
                if key and key not in denylist:
                    denylist[key] = {"reason": "manual", "decided_at": now}
                    result["status"] = "stopped"

            # One registry scan for the whole batch, including unselected siblings.
            retired = []
            for placeholder, raw in entity_map.items():
                key = canonical_key(get_name(raw))
                if key not in names or is_retired(raw):
                    continue
                entity_map[placeholder] = to_storage_value(
                    get_name(raw), existing=raw, retired=True, retired_at=now, retired_reason="owner"
                )
                retired.append(placeholder)
                names[key]["status"] = "stopped"
            Tenant.objects.filter(pk=tenant.pk).update(pii_entity_map=entity_map, pii_denylist=denylist)
        tenant.pii_entity_map = entity_map
        tenant.pii_denylist = denylist
        return Response(
            {
                "results": results,
                "retired": retired,
                "summary": {
                    "requested": len(placeholders),
                    "names_stopped": sum(result["status"] == "stopped" for result in names.values()),
                    "bindings_retired": len(retired),
                },
            }
        )


class EntityRegistryRestoreView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        placeholders = _placeholders(request.data)
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=404)

        with transaction.atomic():
            locked = Tenant.objects.select_for_update().get(pk=tenant.pk)
            entity_map = dict(locked.pii_entity_map or {})
            denylist = dict(locked.pii_denylist or {})
            results = []
            for placeholder in placeholders:
                if placeholder not in entity_map:
                    results.append({"placeholder": placeholder, "status": "not_found"})
                    continue
                raw = entity_map[placeholder]
                result_status = "restored" if is_retired(raw) else "already_active"
                entity_map[placeholder] = to_storage_value(
                    get_name(raw), existing=raw, retired=None, retired_at=None, retired_reason=None
                )
                denylist.pop(canonical_key(get_name(raw)), None)
                results.append({"placeholder": placeholder, "status": result_status})
            Tenant.objects.filter(pk=tenant.pk).update(pii_entity_map=entity_map, pii_denylist=denylist)
        tenant.pii_entity_map = entity_map
        tenant.pii_denylist = denylist
        return Response({"results": results})
