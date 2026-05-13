"""Pillar metadata for the assistant baseline / insights subsystem.

Pillars are stable product concepts (Gravity, Fuel, etc.) tied to UI surfaces,
icon glyphs, and Django apps. Defined as a TextChoices for use as a CharField
choice on every pillar-keyed table. Static metadata (display name, snapshot
cadence) lives in PILLAR_CONFIG below; UI metadata (glyph, color token) is
owned by the frontend.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import models


class Pillar(models.TextChoices):
    GRAVITY = "gravity", "Gravity"
    FUEL = "fuel", "Fuel"
    CORE = "core", "Core"
    LESSONS = "lessons", "Lessons"
    CONSTELLATION = "constellation", "Constellation"
    HORIZONS = "horizons", "Horizons"
    JOURNAL = "journal", "Journal"


@dataclass(frozen=True)
class PillarConfig:
    slug: str
    display_name: str
    default_snapshot_cadence: str  # "weekly" | "daily" | "monthly"


PILLAR_CONFIG: dict[str, PillarConfig] = {
    Pillar.GRAVITY.value: PillarConfig("gravity", "Gravity", "weekly"),
    Pillar.FUEL.value: PillarConfig("fuel", "Fuel", "daily"),
    Pillar.CORE.value: PillarConfig("core", "Core", "daily"),
    Pillar.LESSONS.value: PillarConfig("lessons", "Lessons", "weekly"),
    Pillar.CONSTELLATION.value: PillarConfig("constellation", "Constellation", "weekly"),
    Pillar.HORIZONS.value: PillarConfig("horizons", "Horizons", "weekly"),
    Pillar.JOURNAL.value: PillarConfig("journal", "Journal", "daily"),
}
