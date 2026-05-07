"""Registry for ``workspace/USER.md`` envelope sections.

Each Django app that wants to contribute a section to USER.md declares it
once via :func:`register_section`. The registry then:

1. Sorts sections by ``order`` so render output is deterministic.
2. Auto-wires ``post_save`` and ``post_delete`` on every model in
   ``refresh_on`` to a universal handler that calls
   ``apps.orchestrator.workspace_envelope.push_user_md`` (debounced).

Adding a new envelope contribution becomes one decorator + one model list,
not five places to update. This is the extensibility guarantee for future
pillar work — savings goals, ROSCAs, Fuel tracking-depth, anything we
haven't thought of yet.

Phase 2.6.5 — supersedes the per-pillar signal handlers that lived in each
app's ``signals.py``. Section rendering also moves out of
``workspace_envelope.render_managed_region`` into per-pillar
``envelope.py`` modules, with that function collapsing to a thin loop over
the registry.

Usage::

    # apps/finance/envelope.py
    from apps.orchestrator.envelope_registry import register_section
    from .models import FinanceAccount, FinanceTransaction, PayoffPlan

    @register_section(
        key="finance",
        heading="## Gravity — finance state",
        enabled=lambda t: t.finance_enabled,
        refresh_on=(FinanceAccount, FinanceTransaction, PayoffPlan),
        order=50,
    )
    def render(tenant) -> str:
        ...

The pillar app's ``apps.py:ready()`` then imports the envelope module so
registration happens at app boot, before any agent turn.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Model
from django.db.models.signals import post_delete, post_save

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnvelopeSection:
    """A single section that participates in workspace/USER.md.

    Frozen so registry membership can't be mutated after registration —
    avoids subtle bugs where section A's behavior changes after section B
    registers.
    """

    key: str
    """Unique identifier across all registered sections."""

    heading: str
    """Markdown heading shown in USER.md, e.g. ``"## Gravity — finance state"``."""

    render: Callable[[Tenant], str]
    """Returns the section body markdown, or empty string when there's
    nothing meaningful to show."""

    enabled: Callable[[Tenant], bool]
    """Gating predicate. ``lambda t: True`` for always-on sections,
    ``lambda t: t.finance_enabled`` for feature-flagged ones."""

    refresh_on: tuple[type[Model], ...]
    """Model classes whose ``post_save`` / ``post_delete`` should trigger
    a debounced USER.md refresh. Each model must have a ``tenant_id`` FK
    or a ``tenant`` relation that resolves to one."""

    order: int
    """Display order in USER.md. Lower comes first. Use 10/20/30/... so
    new sections can slot between existing ones without renumbering."""


_REGISTRY: list[EnvelopeSection] = []
_REGISTRY_LOCK = threading.Lock()


def register_section(
    *,
    key: str,
    heading: str,
    enabled: Callable[[Tenant], bool],
    refresh_on: tuple[type[Model], ...] = (),
    order: int,
) -> Callable[[Callable[[Tenant], str]], Callable[[Tenant], str]]:
    """Decorator: register an envelope section.

    The decorated function becomes the section's ``render`` callable.
    Signal handlers are wired on every model in ``refresh_on`` at
    decoration time so registration is fully idempotent.

    Raises ``ValueError`` if ``key`` is already registered — protects
    against accidental duplicate registration via mis-imports.
    """

    def decorator(fn: Callable[[Tenant], str]) -> Callable[[Tenant], str]:
        section = EnvelopeSection(
            key=key,
            heading=heading,
            render=fn,
            enabled=enabled,
            refresh_on=tuple(refresh_on),
            order=order,
        )
        with _REGISTRY_LOCK:
            existing_keys = {s.key for s in _REGISTRY}
            if key in existing_keys:
                raise ValueError(f"envelope section key '{key}' already registered")
            _REGISTRY.append(section)

        for model in section.refresh_on:
            # ``weak=False`` so the receiver isn't garbage-collected — we
            # never need to disconnect, the registry lives for the
            # lifetime of the process.
            post_save.connect(_universal_refresh_receiver, sender=model, weak=False)
            post_delete.connect(_universal_refresh_receiver, sender=model, weak=False)

        return fn

    return decorator


def _universal_refresh_receiver(sender, instance, **kwargs) -> None:
    """post_save / post_delete handler shared across all registered models.

    Resolves the tenant from the instance, schedules a debounced
    ``push_user_md`` on commit. Debounce + idempotency live in
    ``push_user_md``; the receiver just spawns a daemon thread so the
    request thread isn't blocked on the file-share write.
    """
    tenant_id = _resolve_tenant_id(instance)
    if tenant_id is None:
        return

    def _push() -> None:
        # Lazy import — avoids circular imports at module load.
        from apps.orchestrator.workspace_envelope import push_user_md

        try:
            push_user_md(tenant_id)
        except Exception:
            logger.warning(
                "USER.md refresh from registry failed for tenant %s (sender=%s)",
                str(tenant_id)[:8],
                sender.__name__,
                exc_info=True,
            )

    transaction.on_commit(lambda: threading.Thread(target=_push, daemon=True).start())


def _resolve_tenant_id(instance) -> str | None:
    """Return ``str(tenant_id)`` for the given model instance, or None.

    Handles the two common shapes:
      * ``instance.tenant_id`` — direct FK column (most pillar models)
      * ``instance.tenant``    — relation; resolved via ``.id``

    Returns None when neither resolves — the receiver becomes a no-op for
    unexpected sources (e.g., a stray model that someone wrongly added to
    ``refresh_on``).
    """
    direct = getattr(instance, "tenant_id", None)
    if direct is not None:
        return str(direct)
    rel = getattr(instance, "tenant", None)
    if rel is not None and hasattr(rel, "id"):
        return str(rel.id)
    return None


def all_sections() -> list[EnvelopeSection]:
    """Return registered sections sorted by ``order`` ascending.

    ``render_managed_region`` calls this on every USER.md push.
    """
    with _REGISTRY_LOCK:
        return sorted(_REGISTRY, key=lambda s: s.order)


def _reset_registry_for_tests() -> None:
    """Clear the registry. **Tests only.** Resets state between unit tests.

    Production code should never call this — sections are designed to
    register once at app boot and live for the lifetime of the process.
    """
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
