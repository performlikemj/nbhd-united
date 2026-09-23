"""Strict, content-free TypeSafe Decisions door; callers own decision policy."""

import os
import sys
from enum import Enum
from typing import Annotated, Literal, get_args
from unittest.mock import Mock

import requests
from django.conf import settings
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

JEV_MODEL = "typesafe/jev-1.13"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Description = Annotated[str, Field(min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChoiceQuestion(StrictModel):
    type: Literal["choice"] = "choice"
    instructions: Description
    criteria: dict[str, Description] = Field(min_length=2)


class NoulQuestion(StrictModel):
    type: Literal["noul"] = "noul"
    instructions: Description
    criteria: dict[Literal["true", "false"], Description] | None = None


class ScoreQuestion(StrictModel):
    type: Literal["score"] = "score"
    instructions: Description
    criteria: list[Description] = Field(min_length=2, max_length=10)


Question = Annotated[ChoiceQuestion | NoulQuestion | ScoreQuestion, Field(discriminator="type")]
Questions = TypeAdapter(dict[str, Question])


def choice_question(annotation, instructions, descriptions):
    """Take option identity from a Pydantic field's Literal or a string Enum."""
    options = (
        [item.value for item in annotation]
        if isinstance(annotation, type) and issubclass(annotation, Enum)
        else list(get_args(annotation))
    )
    if not options or set(options) != set(descriptions):
        raise ValueError("Choice descriptions must cover exactly the typed options")
    return ChoiceQuestion(instructions=instructions, criteria={value: descriptions[value] for value in options})


class ChoiceAnswer(StrictModel):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, Probability] = Field(min_length=2)
    confidence: Probability

    @model_validator(mode="after")
    def distribution(self):
        if abs(sum(self.probabilities.values()) - 1) > 0.01:
            raise ValueError("Invalid probability total")
        if self.choice not in self.probabilities or self.probabilities[self.choice] < max(self.probabilities.values()):
            raise ValueError("Choice is not a highest-probability option")
        return self


class NoulAnswer(StrictModel):
    type: Literal["noul"]
    noul: Probability

    @property
    def confidence(self):
        # Noul has no separate provider confidence. Gate the chosen polarity.
        return max(self.noul, 1 - self.noul)


class ScoreAnswer(StrictModel):
    type: Literal["score"]
    score: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    legend: dict[str, Description]
    probabilities: dict[str, Probability]
    confidence: Probability

    @model_validator(mode="after")
    def distribution(self):
        keys = {str(i) for i in range(len(self.legend))}
        if len(keys) < 2 or set(self.legend) != keys or set(self.probabilities) != keys:
            raise ValueError("Invalid score levels")
        expected = sum(int(key) * value for key, value in self.probabilities.items())
        if (
            abs(sum(self.probabilities.values()) - 1) > 0.01
            or self.score > len(keys) - 1
            or abs(expected - self.score) > 0.02
        ):
            raise ValueError("Invalid score distribution")
        return self


Answer = Annotated[ChoiceAnswer | NoulAnswer | ScoreAnswer, Field(discriminator="type")]


class DecisionUsage(StrictModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost: float = Field(ge=0, allow_inf_nan=False)


class DecisionResponse(StrictModel):
    model: str
    answers: dict[str, Answer]
    usage: DecisionUsage
    id: str
    provider: str


class JevUnavailable(ValueError):
    """Content-free error: callers must use their previous fallback."""


def decide(state, questions) -> DecisionResponse:
    """One POST, no retries. Tests can enter only through a mocked transport.

    Decisions uses typed question primitives as its provider schema, rather
    than the chat-completions response_format protocol.
    """
    testing = (
        getattr(settings, "TEST_MODE", False)
        or "test" in sys.argv
        or "pytest" in sys.modules
        or "PYTEST_CURRENT_TEST" in os.environ
    )
    if testing and not isinstance(requests.post, Mock):
        raise JevUnavailable("Jev unavailable")
    try:
        key = settings.OPENROUTER_API_KEY
        if not key:
            raise ValueError("Missing key")
        questions = Questions.validate_python(questions)
        if not questions:
            raise ValueError("Missing questions")
        body = {
            "model": JEV_MODEL,
            "state": state,
            "questions": {name: question.model_dump(exclude_none=True) for name, question in questions.items()},
        }
        response = requests.post(
            DECISIONS_URL,
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=(2, 4),
            allow_redirects=False,
        )
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise ValueError("Unexpected HTTP status")
        result = DecisionResponse.model_validate_json(response.text)
        if result.model != JEV_MODEL and not result.model.startswith(JEV_MODEL + "-"):
            raise ValueError("Unexpected model")
        if set(result.answers) != set(questions):
            raise ValueError("Incomplete or unexpected answers")
        for name, question in questions.items():
            answer = result.answers[name]
            if answer.type != question.type:
                raise ValueError("Unexpected answer type")
            if isinstance(answer, ChoiceAnswer) and set(answer.probabilities) != set(question.criteria):
                raise ValueError("Unexpected choice options")
            if isinstance(answer, ScoreAnswer) and answer.legend != {
                str(i): value for i, value in enumerate(question.criteria)
            }:
                raise ValueError("Unexpected score legend")
        return result
    except Exception:
        # Pydantic and HTTP exceptions can contain raw state/provider output.
        raise JevUnavailable("Jev unavailable") from None
