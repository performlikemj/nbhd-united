"""Fetch available models from LLM providers using the user's API key."""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 10


def _openai_models(api_key: str) -> list[dict[str, Any]]:
    resp = requests.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    models = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        # Only chat-capable models
        if any(t in mid for t in ("gpt", "o1", "o3", "o4", "codex", "chatgpt")):
            models.append({
                "id": f"openai/{mid}",
                "name": mid,
                "context_window": None,
            })
    models.sort(key=lambda x: x["name"])
    return models


def _anthropic_models(api_key: str) -> list[dict[str, Any]]:
    resp = requests.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    models = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        models.append({
            "id": f"anthropic/{mid}",
            "name": m.get("display_name", mid),
            "context_window": m.get("context_window"),
        })
    models.sort(key=lambda x: x["name"])
    return models


def _groq_models(api_key: str) -> list[dict[str, Any]]:
    resp = requests.get(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    models = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        ctx = m.get("context_window")
        models.append({
            "id": f"groq/{mid}",
            "name": mid,
            "context_window": ctx,
        })
    models.sort(key=lambda x: x["name"])
    return models


def _google_models(api_key: str) -> list[dict[str, Any]]:
    resp = requests.get(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    models = []
    for m in resp.json().get("models", []):
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            continue
        # name is "models/gemini-..." — strip prefix
        raw_name = m.get("name", "")
        mid = raw_name.replace("models/", "")
        models.append({
            "id": f"google/{mid}",
            "name": m.get("displayName", mid),
            "context_window": m.get("inputTokenLimit"),
        })
    models.sort(key=lambda x: x["name"])
    return models


def _openrouter_models(api_key: str) -> list[dict[str, Any]]:
    resp = requests.get(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    models = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        ctx = m.get("context_length")
        models.append({
            "id": f"openrouter/{mid}",
            "name": m.get("name", mid),
            "context_window": ctx,
        })
    models.sort(key=lambda x: x["name"])
    return models


def _xai_models(api_key: str) -> list[dict[str, Any]]:
    resp = requests.get(
        "https://api.x.ai/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    models = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        models.append({
            "id": f"xai/{mid}",
            "name": mid,
            "context_window": None,
        })
    models.sort(key=lambda x: x["name"])
    return models


_FETCHERS = {
    "openai": _openai_models,
    "anthropic": _anthropic_models,
    "groq": _groq_models,
    "google": _google_models,
    "openrouter": _openrouter_models,
    "xai": _xai_models,
}


def fetch_models(provider: str, api_key: str) -> list[dict[str, Any]]:
    """Fetch available models for *provider* using *api_key*.

    Returns a list of dicts: ``[{"id": ..., "name": ..., "context_window": ...}]``

    Raises:
        ValueError: unsupported provider
        requests.HTTPError: provider returned an error (e.g. 401)
        requests.Timeout: provider didn't respond in time
    """
    fetcher = _FETCHERS.get(provider)
    if not fetcher:
        raise ValueError(f"Unsupported provider: {provider}")
    return fetcher(api_key)
