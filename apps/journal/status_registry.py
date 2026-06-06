"""Plug-in registry for Journal current-status providers.

The proactive/cron layer grounds on a single authoritative snapshot of the
user's *current* state. To make sure a new feature is never left out of that
snapshot, features are not hand-wired into one central function — each
registers a provider here, and ``build_journal_status`` returns the union of
every *enabled* provider's contribution. Add a feature → it ships with a
provider → it is in the snapshot automatically.

Provider contract
-----------------
* ``key`` — stable, unique domain name (e.g. ``"tasks"``, ``"finance"``).
* ``enabled(tenant) -> bool`` — per-tenant gate. A disabled/paused/absent
  domain contributes nothing, so the assistant stays silent about it rather
  than guessing — the safe default.
* ``provide(tenant, today) -> dict`` — returns a JSON-serializable dict that is
  merged into the snapshot. It MUST:
    - scope every read to ``tenant`` (Postgres RLS is the backstop, not the
      primary control);
    - read through the Django ORM only — parameterized queries, never SQL
      built from strings (no injection surface);
    - return data, not instructions — the snapshot is reported to the LLM as
      untrusted content, never executed.

Built-in providers (tasks, goals, finance) register in ``status_projection``.
A provider that lives in another app should register from that app's
``AppConfig.ready()`` so it is present before any snapshot is built. The
``test_status_registry`` suite fails the build if a built-in domain loses its
provider, and proves a freshly-registered provider is picked up with no change
to ``build_journal_status``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.tenants.models import Tenant


@dataclass(frozen=True)
class StatusProvider:
    key: str
    enabled: Callable[[Tenant], bool]
    provide: Callable[[Tenant, date], dict]


_PROVIDERS: dict[str, StatusProvider] = {}


def register_status_provider(
    key: str,
    *,
    enabled: Callable[[Tenant], bool],
    provide: Callable[[Tenant, date], dict],
) -> None:
    """Register (or replace) the status provider for ``key``.

    Replacement is allowed so module re-import is idempotent; two *different*
    features must use distinct keys.
    """
    _PROVIDERS[key] = StatusProvider(key=key, enabled=enabled, provide=provide)


def unregister_status_provider(key: str) -> None:
    """Remove a provider (used by tests that register a temporary provider)."""
    _PROVIDERS.pop(key, None)


def status_providers() -> list[StatusProvider]:
    return list(_PROVIDERS.values())


def registered_keys() -> set[str]:
    return set(_PROVIDERS)
