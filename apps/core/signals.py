"""Signals for the Core pillar.

USER.md refresh on MeditationSession changes is auto-wired by the envelope
registry (apps/core/envelope.py registers MeditationSession as a ``refresh_on``
trigger). No USER.md push handlers belong here — that path is owned by the
registry. This module is the home for any future Core-specific side effects
(cache-tag bumps, delivery hooks) and gives apps.py ``ready()`` a stable import.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
