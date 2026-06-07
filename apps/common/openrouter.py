"""Shared OpenRouter chat-completion client with multi-model fallback.

The platform makes a handful of its own OpenRouter calls (Gravity synthesis,
journal extraction, PII arbiter, agenda hints). Historically each one POSTed a
single hard-coded model with no fallback, so any OpenRouter hiccup on that model
silently broke the feature. This wrapper tries an ordered list of models until
one answers, and records per-model health into ``ModelHealth`` so a degraded
model is visible (and so the free-offer monitor has signal from real traffic).

Model ids are accepted in OpenClaw form (``openrouter/deepseek/deepseek-v4-pro``)
to match ``apps.billing.constants``; ``normalize_model_id`` strips the
``openrouter/`` routing prefix before the bare slug hits the OpenRouter HTTP API.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


def normalize_model_id(model_id: str) -> str:
    """Strip OpenClaw's ``openrouter/`` routing prefix for the HTTP API.

    OpenClaw config uses ``openrouter/<provider>/<model>``; the OpenRouter REST
    API expects the bare ``<provider>/<model>`` slug. Other prefixes
    (``anthropic/``, ``openai/``) are real OpenRouter slugs and pass through
    untouched.
    """
    return model_id.removeprefix("openrouter/")


def _looks_usable(data: dict) -> bool:
    """A 200 response is only usable if it carries assistant content. OpenRouter
    can return HTTP 200 with a top-level ``error`` (rate limit, upstream
    failure) and no choices — treat that as a failure so we fall through."""
    if not isinstance(data, dict):
        return False
    if data.get("error"):
        return False
    choices = data.get("choices")
    if not choices:
        return False
    content = (choices[0] or {}).get("message", {}).get("content")
    return bool(content)


def chat_completion(
    models: str | list[str],
    messages: list[dict[str, Any]],
    *,
    api_key: str | None = None,
    timeout: int = 45,
    record_health: bool = True,
    **body_params: Any,
) -> tuple[dict, str]:
    """POST to OpenRouter chat/completions, trying each model in order.

    Args:
        models: Ordered list of OpenClaw-form model ids (primary first), or a
            single id. Falsy entries are skipped.
        messages: OpenAI-style messages list.
        api_key: OpenRouter key; defaults to ``settings.OPENROUTER_API_KEY``.
        timeout: Per-attempt request timeout in seconds.
        record_health: When True, write success/failure to ``ModelHealth``.
        **body_params: Extra request body fields (``max_tokens``,
            ``temperature``, ``response_format``, …).

    Returns:
        ``(response_json, model_used)`` where ``model_used`` is the OpenClaw-form
        id that succeeded.

    Raises:
        RuntimeError: if no API key is configured.
        Exception: the last error encountered if every candidate fails (so the
            caller's existing error handling still fires).
    """
    key = api_key if api_key is not None else getattr(settings, "OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    candidates = [m for m in ([models] if isinstance(models, str) else list(models)) if m]
    if not candidates:
        raise ValueError("chat_completion requires at least one model")

    last_error: Exception | None = None
    for model_id in candidates:
        try:
            resp = requests.post(
                OPENROUTER_CHAT_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": normalize_model_id(model_id),
                    "messages": messages,
                    **body_params,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if not _looks_usable(data):
                raise RuntimeError(f"OpenRouter returned no usable choices for {model_id}: {str(data)[:200]}")
        except Exception as exc:  # noqa: BLE001 — record + try the next candidate
            last_error = exc
            if record_health:
                _record_failure(model_id, repr(exc))
            logger.warning(
                "OpenRouter call failed on %s (%d candidate(s) total); %s",
                model_id,
                len(candidates),
                "trying next" if model_id != candidates[-1] else "no fallback left",
            )
            continue
        if record_health:
            _record_success(model_id)
        return data, model_id

    assert last_error is not None  # candidates is non-empty, so we tried ≥1
    raise last_error


def _record_success(model_id: str) -> None:
    try:
        from apps.billing.model_offers import record_model_success

        record_model_success(model_id)
    except Exception:  # noqa: BLE001 — health recording must never break a real call
        logger.debug("model health: failed to record success for %s", model_id, exc_info=True)


def _record_failure(model_id: str, error: str) -> None:
    try:
        from apps.billing.model_offers import record_model_failure

        record_model_failure(model_id, error)
    except Exception:  # noqa: BLE001
        logger.debug("model health: failed to record failure for %s", model_id, exc_info=True)
