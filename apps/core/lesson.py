"""Structured meditation authoring contract and the shared lesson vocabulary."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TRADITIONS: tuple[str, ...] = (
    "stoic",
    "zen",
    "buddhist",
    "taoist",
    "sufi",
    "christian_contemplative",
    "jewish",
    "secular_philosophy",
    "science_of_mind",
    "other",
)
Tradition = Literal[TRADITIONS]


def normalize_teaching_slug(value: str) -> str:
    """Comparison key: punctuation and casing cannot bypass the variety gate."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


class MeditationLesson(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tradition: Tradition
    teaching_slug: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    core_teaching: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=400)
    practice: str = Field(min_length=1, max_length=200)

    @field_validator("teaching_slug", mode="before")
    @classmethod
    def normalize_slug(cls, value):
        if isinstance(value, str):
            return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
        return value


LESSON_JSON_SCHEMA = MeditationLesson.model_json_schema()


class MeditationManifest(BaseModel):
    """Keep the existing phase grammar in render.validate_manifest."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    title: str
    theme: str
    voice: str
    global_tone: str
    total_target_seconds: int
    ambient: dict | str | None
    phases: list[dict] = Field(max_length=7)
    lesson: MeditationLesson
