"""Strict, content-free TypeSafe Decisions door; callers own decision policy."""

import os
import sys
from enum import Enum
from time import monotonic
from typing import Annotated, Literal, get_args

import requests
from django.conf import settings
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from urllib3.util import Timeout

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


def _running_tests() -> bool:
    if getattr(settings, "TEST_MODE", False) or "test" in sys.argv or "PYTEST_CURRENT_TEST" in os.environ:
        return True
    # Inspect active runners, including the parent of a deadline worker. Merely
    # importing/installing unittest or pytest in production is not a test run.
    for frame in sys._current_frames().values():
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            if module in {"unittest.case", "unittest.suite", "unittest.runner"} or module.startswith("_pytest."):
                return True
            frame = frame.f_back
    return False


def _post(*args, **kwargs):
    """The real transport is always forbidden inside a test runner.

    Tests replace THIS function with an offline fake. Wrapping this function or
    requests.post with a spy still enters this guard and cannot reach requests.
    """
    if _running_tests():
        raise JevUnavailable("Jev unavailable")
    return requests.post(*args, **kwargs)


def decide(state, questions, *, deadline: float | None = None) -> DecisionResponse:
    """One POST, no retries; an optional monotonic deadline caps the I/O budget.

    Decisions uses typed question primitives as its provider schema, rather
    than the chat-completions response_format protocol. The shape caller also
    bounds wall time, including DNS and response parsing, outside requests.
    """
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
        timeout = (2, 4)
        if deadline is not None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise JevUnavailable("Jev unavailable")
            timeout = Timeout(total=remaining, connect=min(2, remaining), read=min(4, remaining))
        response = _post(
            DECISIONS_URL,
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=timeout,
            allow_redirects=False,
        )
        if deadline is not None and monotonic() >= deadline:
            raise JevUnavailable("Jev unavailable")
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
        if deadline is not None and monotonic() >= deadline:
            raise JevUnavailable("Jev unavailable")
        return result
    except Exception:
        # Pydantic and HTTP exceptions can contain raw state/provider output.
        raise JevUnavailable("Jev unavailable") from None
