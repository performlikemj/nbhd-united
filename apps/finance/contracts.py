"""Model-facing 400 envelopes for the finance tools.

Every rejection the finance runtime emits has to be *correctable by the model
that made the call*, because the alternative — what this module replaces — was a
silent coercion that produced a plausible-looking success with the wrong number
in it (a payment into savings clamped to zero, a transfer echoing a stale
balance as ``new_balance``).

The shape matches ``apps.common.llm_contracts.LLMValidationError`` so the runtime
self-correct loop treats these the same as a Pydantic failure: ``error`` is the
machine code, ``message`` says what to do instead, and ``details`` carries one
entry per offending field with the field path in ``loc``. Extra machine-readable
keys (``allowed``, ``candidates``, ``received``) ride inside the detail entry.

``FinanceInputError`` exists so the shared service layer can reject a write that
both the runtime view and the JWT consumer view perform. Each caller decides its
own HTTP framing and — for the runtime path only — its telemetry.
"""

from __future__ import annotations

from typing import Any


def tool_error(
    code: str,
    message: str,
    *,
    field: str,
    msg: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build one ``LLMValidationError``-shaped payload for a single bad field."""
    detail: dict[str, Any] = {
        "loc": [field],
        "msg": msg or message,
        "type": "value_error",
    }
    detail.update({key: value for key, value in extra.items() if value is not None})
    return {"error": code, "message": message, "details": [detail]}


class FinanceInputError(Exception):
    """A finance write the caller must correct before it can be recorded.

    Carries the ready-to-serialise envelope plus the flags the runtime view
    needs for telemetry — never the values themselves, so a nickname or a
    balance cannot reach the event table through this object.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str,
        reason_code: str | None = None,
        telemetry: dict[str, Any] | None = None,
        **extra: Any,
    ):
        super().__init__(message)
        self.code = code
        self.field = field
        self.reason_code = reason_code or code
        self.telemetry = telemetry or {}
        self.payload = tool_error(code, message, field=field, **extra)

    def as_tool_result(self) -> dict[str, Any]:
        return self.payload
