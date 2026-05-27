"""Typed cron pattern registry.

Each pattern module registers itself on import via ``register_handler()``.
Consumers (service layer, runtime endpoints, signals, enforcement plugin
RPC) look up handlers by pattern name with ``get_handler()``.

See CONTINUITY_cron-typed-patterns.md for the architectural rationale and
the per-pattern toolsAllow / validation contract.
"""

from __future__ import annotations

from .base import PatternHandler, PatternPayload

_REGISTRY: dict[str, PatternHandler] = {}


def register_handler(handler: PatternHandler) -> PatternHandler:
    if handler.pattern in _REGISTRY:
        existing = _REGISTRY[handler.pattern]
        if existing is handler or type(existing) is type(handler):
            return existing
        raise RuntimeError(
            f"Duplicate handler registration for pattern={handler.pattern!r}: "
            f"existing={type(existing).__name__}, new={type(handler).__name__}"
        )
    _REGISTRY[handler.pattern] = handler
    return handler


def get_handler(pattern: str) -> PatternHandler:
    if pattern not in _REGISTRY:
        raise KeyError(f"Unknown cron pattern: {pattern!r}. Registered: {sorted(_REGISTRY.keys())}")
    return _REGISTRY[pattern]


def list_handlers() -> dict[str, PatternHandler]:
    return dict(_REGISTRY)


# Trigger registration of all handler modules. Each module calls
# register_handler() at import time. Order doesn't matter — names are unique.
from . import (
    daily_briefing,  # noqa: E402, F401
    domain_summary,  # noqa: E402, F401
    pure_reminder,  # noqa: E402, F401
    quote_user_intent,  # noqa: E402, F401
)

__all__ = [
    "PatternHandler",
    "PatternPayload",
    "get_handler",
    "list_handlers",
    "register_handler",
]
