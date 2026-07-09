"""Async LLM naming pass for lesson constellations.

The deterministic labeler in ``clustering.py`` is the always-on floor: it runs
synchronously inside ``refresh_constellation`` and produces a Title-Case name
from tags/text for every cluster. This module is the *upgrade* layered on top —
a small, cheap, warm LLM (platform-side Haiku) that turns "Fitness & Health"
into "Strength Training" when the evidence supports something more concrete.

It mirrors ``apps/lessons/copilot.py`` for the egress machinery on purpose:
  * Platform OpenRouter key (``OPENROUTER_API_KEY``), one ``requests.post`` with
    a timeout, usage attributed ``is_system=True`` so naming never eats a
    tenant's quota.
  * PII never egresses raw: every snippet + the deterministic label + top terms
    are redacted through a ``RedactionSession`` before the call; the returned
    name is rehydrated against the tenant map ∪ this call's fresh mints and any
    residual ``[TYPE_N]`` placeholder is scrubbed.
  * Kill-switch ``CLUSTER_LABEL_LLM_ENABLED`` (default True via settings/getattr,
    so no Azure env change is required to ship) and a deterministic fallback on
    ANY failure. Labels must NEVER block or fail clustering — every failure path
    leaves the deterministic label already written by ``generate_cluster_labels``.

Cost control: results are cached on ``Tenant.cluster_label_cache`` keyed by a
hash of a cluster's SORTED MEMBER LESSON IDS (cluster_id numbers are reassigned
every recluster, so they can't be the key). A cache hit reuses the stored name
with no LLM call; a miss makes ≤1 call, capped at ``MAX_CALLS_PER_RUN`` per task
run (excess clusters keep their deterministic labels until the next run). Cache
entries whose member-hash no longer exists are pruned each run.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from typing import Any

import requests
from django.conf import settings

from apps.pii.redactor import RedactionSession, rehydrate_text

from .clustering import _cluster_candidate_terms, deterministic_cluster_label

logger = logging.getLogger(__name__)

# ── Tuning ──────────────────────────────────────────────────────────────────
MAX_CALLS_PER_RUN = 5  # LLM calls per task run; excess clusters keep det. labels
SNIPPETS_PER_CLUSTER = 3  # short redacted snippets handed to the model
TERMS_PER_CLUSTER = 6  # top deterministic terms/tags handed to the model
_SNIPPET_CAP = 160  # per-snippet char cap before egress
_NAME_MAX_LEN = 40  # DB label cap (matches deterministic labeler)
_NAME_MAX_WORDS = 4  # "2-4 word Title Case name"

# One record-separator the redactor won't treat as PII — lets us redact every
# free-text field for a cluster in a single model pass, then split back.
_SENTINEL = "\n␞\n"

# Any residual ``[TYPE_N]`` placeholder — scrubbed at the egress boundary.
_PLACEHOLDER_RE = re.compile(r"\[[A-Z_]+_\d+\]")


def _cluster_label_model() -> str:
    """Model id for cluster names — small, fast, cheap. Overridable via settings."""
    return getattr(settings, "CLUSTER_LABEL_MODEL", "anthropic/claude-haiku-4.5")


def _cluster_label_llm_enabled() -> bool:
    """Kill-switch for the live LLM naming call. Off → deterministic labels stand."""
    return bool(getattr(settings, "CLUSTER_LABEL_LLM_ENABLED", True))


# ── System prompt ───────────────────────────────────────────────────────────
NAMING_SYSTEM = """You name one constellation in a person's map of life-lessons. Each constellation
is a cluster of related lessons they've learned. You are given a working label, the top
keywords/tags, and a few short lesson snippets.

Return a 2-4 word name in Title Case that captures the concrete THEME of the cluster.
- Prefer the concrete real-world domain over generic self-improvement vocabulary. Say
  "Strength Training", "Job Search", "Home Cooking", "Sleep Habits" — not "Growth",
  "Consistency", "Discipline", "Self Improvement", or "Personal Development".
- No personal names, no quotes, no punctuation, no trailing period. No emoji.
- Do not use the "·" character.

Return only the name — nothing else."""


def _member_hash(lesson_ids: list[int]) -> str:
    """Stable cache key for a cluster: sha1 of its sorted member lesson ids."""
    joined = ",".join(str(i) for i in sorted(lesson_ids))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _cap(text: str | None) -> str:
    text = (text or "").strip()
    return text[: _SNIPPET_CAP - 1] + "…" if len(text) > _SNIPPET_CAP else text


def _clean_name(name: str) -> str:
    """Normalise a model name into a safe display label.

    Strips wrapping quotes/labels, drops record separators, clamps to
    ``_NAME_MAX_WORDS`` Title-Case words, and caps length at ``_NAME_MAX_LEN``.
    """
    name = (name or "").strip()
    # Strip a leading "Name:" style prefix a model sometimes adds.
    name = re.sub(r"^[a-zA-Z ]{0,20}:\s*", "", name).strip()
    if len(name) >= 2 and name[0] in "\"'“‘" and name[-1] in "\"'”’":
        name = name[1:-1].strip()
    name = name.replace("·", " ")
    words = [w for w in re.split(r"\s+", name) if w][:_NAME_MAX_WORDS]
    cleaned = " ".join(w.title() for w in words)
    return cleaned[:_NAME_MAX_LEN].strip()


def _scrub_placeholders(text: str) -> str:
    """Drop any residual ``[TYPE_N]`` token a rehydration map didn't cover.

    The PII contract is that no such token ever reaches a stored label. Replace
    it with a neutral word rather than leak it.
    """
    if not text or "[" not in text:
        return text
    leftovers = _PLACEHOLDER_RE.findall(text)
    if not leftovers:
        return text
    logger.warning("cluster_naming: scrubbed %d residual PII placeholder(s) from a name", len(leftovers))
    cleaned = _PLACEHOLDER_RE.sub("Theme", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _redact_fields(session: RedactionSession, fields: list[str]) -> list[str]:
    """Redact a list of free-text fields through one shared session in a single pass.

    Batched via a sentinel join so the heavy NER model runs once per cluster.
    On any split mismatch, falls back to redacting each field individually so a
    sentinel hiccup can never corrupt or leak text.
    """
    if not fields:
        return []
    joined = _SENTINEL.join(fields)
    parts = session.redact(joined).split(_SENTINEL)
    if len(parts) == len(fields):
        return parts
    return [session.redact(f) for f in fields]


def _build_messages(label: str, terms: list[str], snippets: list[str]) -> list[dict[str, str]]:
    lines = [f'Working label: "{label}"']
    if terms:
        lines.append("Top keywords/tags: " + ", ".join(terms))
    if snippets:
        lines.append("Lesson snippets:")
        lines.extend(f'- "{s}"' for s in snippets)
    lines.append("\nNow return the constellation name.")
    return [
        {"role": "system", "content": NAMING_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _resolve_api_key() -> str:
    key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not key:
        raise ValueError("OPENROUTER_API_KEY is not configured")
    return key


def _cluster_naming_request(messages: list[dict], *, tenant_id: str | None = None) -> str:
    """One naming LLM call → the plain-text name. Records system-attributed usage.

    Raises on any HTTP / parse failure so the caller can fall back. Tests patch
    this directly (set ``.return_value`` to a string).
    """
    model = _cluster_label_model()
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {_resolve_api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 20,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    if tenant_id:
        _record_naming_usage(tenant_id, model, data.get("usage", {}) or {})

    return (data["choices"][0]["message"]["content"] or "").strip()


def _record_naming_usage(tenant_id: str, model: str, usage: dict) -> None:
    """Attribute a naming call's spend to the tenant (system-side). Never raises."""
    try:
        from apps.billing.services import record_usage
        from apps.tenants.models import Tenant

        tenant = Tenant.objects.filter(id=tenant_id).first()
        if tenant is None:
            return
        record_usage(
            tenant,
            event_type="cluster_label",
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model_used=model,
            # Platform-side call: tracked for visibility, never counted against
            # the tenant's quota — naming their galaxy must not lock them out.
            is_system=True,
        )
    except Exception:
        logger.exception("cluster_naming: usage record failed for tenant %s", str(tenant_id)[:8])


def _name_one_cluster(
    tenant,
    members: list,
    deterministic: str,
    terms: list[str],
) -> str | None:
    """Redact evidence → LLM → rehydrate/scrub → clean. Returns a name or None.

    None means "keep the deterministic label" — any failure (LLM error, empty
    reply, all-placeholder name) degrades cleanly.
    """
    # mint='never': cluster labels/terms/snippets are derived evidence, the same
    # class the prod audit flagged as a junk-mint source (copilot/memory-sync).
    # Known people are still masked; unfamiliar machine-derived strings no longer
    # coin new bindings before this egresses to the naming LLM.
    session = RedactionSession(tenant=tenant, mint="never")

    snippets = [_cap(m.text) for m in members if (m.text or "").strip()][:SNIPPETS_PER_CLUSTER]
    fields = [deterministic, *terms[:TERMS_PER_CLUSTER], *snippets]
    redacted = _redact_fields(session, fields)
    r_label = redacted[0]
    r_terms = redacted[1 : 1 + len(terms[:TERMS_PER_CLUSTER])]
    r_snips = redacted[1 + len(terms[:TERMS_PER_CLUSTER]) :]

    try:
        raw = _cluster_naming_request(_build_messages(r_label, r_terms, r_snips), tenant_id=str(tenant.id))
    except Exception:
        logger.info(
            "cluster_naming: LLM failed for tenant %s — deterministic label kept", str(tenant.id)[:8], exc_info=True
        )
        return None

    # Rehydrate + scrub BEFORE cleaning: _clean_name Title-Cases the text, which
    # would lowercase an uppercase ``[PERSON_1]`` placeholder and defeat both the
    # rehydration regex and the scrub. So restore/strip PII first, then clean.
    name = (raw or "").strip()
    rehydrate_map = {**(getattr(tenant, "pii_entity_map", None) or {}), **session.entity_map}
    if rehydrate_map:
        name = rehydrate_text(name, rehydrate_map)
    name = _clean_name(_scrub_placeholders(name))
    return name or None


def name_clusters_for_tenant(tenant_id: str) -> dict[str, Any]:
    """Name a tenant's clusters with the LLM, cached + capped. Never raises.

    Returns a counts-only status dict (no lesson content — surfaces in QStash
    dashboards). Writes accepted names to ``cluster_label`` for each cluster's
    lessons; any cluster we don't reach keeps the deterministic label.
    """
    from apps.lessons.models import Lesson
    from apps.tenants.models import Tenant

    if not _cluster_label_llm_enabled():
        return {"skipped": "disabled"}

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if tenant is None:
        return {"skipped": "no_tenant"}

    clustered = list(Lesson.objects.filter(tenant=tenant, status="approved", cluster_id__isnull=False))
    if not clustered:
        return {"clusters": 0, "named": 0, "cache_hits": 0, "calls": 0}

    # total_docs + global tag DF over ALL approved lessons — mirrors the
    # deterministic labeler so the evidence terms match what shipped.
    all_lessons = list(Lesson.objects.filter(tenant=tenant, status="approved"))
    total_docs = len(all_lessons) or 1
    global_tag_df: Counter = Counter()
    for lesson in all_lessons:
        global_tag_df.update(set(lesson.tags))

    clusters: dict[int, list] = {}
    for lesson in clustered:
        clusters.setdefault(lesson.cluster_id, []).append(lesson)

    cache = dict(getattr(tenant, "cluster_label_cache", None) or {})
    live_hashes: set[str] = set()
    calls = 0
    cache_hits = 0
    named = 0

    for cluster_id, members in clusters.items():
        member_hash = _member_hash([m.id for m in members])
        live_hashes.add(member_hash)

        if member_hash in cache:
            name = cache[member_hash]
            cache_hits += 1
        else:
            if calls >= MAX_CALLS_PER_RUN:
                continue  # keep deterministic label; try again next run
            deterministic = deterministic_cluster_label(members, total_docs, global_tag_df)
            terms, _used_tags = _cluster_candidate_terms(members, total_docs, global_tag_df)
            name = _name_one_cluster(tenant, members, deterministic, terms)
            calls += 1
            if not name:
                continue  # LLM failed / empty → deterministic label stays
            cache[member_hash] = name

        Lesson.objects.filter(
            tenant=tenant,
            status="approved",
            cluster_id=cluster_id,
        ).update(cluster_label=name)
        named += 1

    # Prune cache entries for member-hashes that no longer exist.
    cache = {h: v for h, v in cache.items() if h in live_hashes}
    tenant.cluster_label_cache = cache
    tenant.save(update_fields=["cluster_label_cache"])

    return {"clusters": len(clusters), "named": named, "cache_hits": cache_hits, "calls": calls}
