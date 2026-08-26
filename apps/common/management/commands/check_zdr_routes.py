"""Verify model endpoint ZDR eligibility after deploys or before model changes.

Run this post-deploy and before changing either the configured STT model or the
embedding model ID; it fails closed unless every advertised endpoint for both
models appears in OpenRouter's current ZDR endpoint inventory.
"""

from __future__ import annotations

from urllib.parse import quote

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.lessons.services import EMBEDDING_MODEL

_OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
_DEFAULT_STT_MODEL = "openai/whisper-large-v3-turbo"


class Command(BaseCommand):
    """Run post-deploy and before changing either STT or embedding model IDs."""

    help = "Fail unless every STT and embedding endpoint is listed as ZDR by OpenRouter."

    def handle(self, *args, **options):
        del args, options
        key = str(getattr(settings, "OPENROUTER_API_KEY", "") or "").strip()
        if not key:
            raise CommandError("OPENROUTER_API_KEY is not configured")

        stt_model = str(getattr(settings, "OPENROUTER_STT_MODEL", _DEFAULT_STT_MODEL) or _DEFAULT_STT_MODEL).strip()
        models = tuple(dict.fromkeys((stt_model, EMBEDDING_MODEL)))
        headers = {"Authorization": f"Bearer {key}"}

        try:
            advertised = {model: self._fetch_model_providers(model, headers) for model in models}
            zdr_routes = self._fetch_zdr_routes(headers)
        except (requests.RequestException, ValueError, TypeError, KeyError):
            raise CommandError("Unable to verify OpenRouter ZDR routes") from None

        failures: dict[str, list[str]] = {}
        for model, providers in advertised.items():
            missing = sorted(provider for provider in providers if (model, provider) not in zdr_routes)
            if missing:
                failures[model] = missing

        if failures:
            for model, providers in failures.items():
                self.stderr.write(f"{model} -> {', '.join(providers)}")
            raise CommandError("OpenRouter has non-ZDR endpoints for configured models")

        self.stdout.write(self.style.SUCCESS(f"ZDR routes verified for {len(models)} model(s)."))

    @staticmethod
    def _fetch_model_providers(model: str, headers: dict[str, str]) -> set[str]:
        encoded_model = quote(model, safe="/")
        response = requests.get(
            f"{_OPENROUTER_API_BASE}/models/{encoded_model}/endpoints",
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        endpoints = data.get("endpoints") if isinstance(data, dict) else None
        if not isinstance(endpoints, list) or not endpoints:
            raise ValueError(f"No endpoints returned for {model}")
        providers = {
            endpoint.get("provider_name", "").strip()
            for endpoint in endpoints
            if isinstance(endpoint, dict) and isinstance(endpoint.get("provider_name"), str)
        }
        if not providers or "" in providers:
            raise ValueError(f"Malformed endpoints returned for {model}")
        return providers

    @staticmethod
    def _fetch_zdr_routes(headers: dict[str, str]) -> set[tuple[str, str]]:
        response = requests.get(
            f"{_OPENROUTER_API_BASE}/endpoints/zdr",
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise ValueError("OpenRouter returned no ZDR endpoints")
        routes = {
            (endpoint.get("model_id", "").strip(), endpoint.get("provider_name", "").strip())
            for endpoint in data
            if isinstance(endpoint, dict)
            and isinstance(endpoint.get("model_id"), str)
            and isinstance(endpoint.get("provider_name"), str)
        }
        if not routes or any(not model or not provider for model, provider in routes):
            raise ValueError("OpenRouter returned malformed ZDR endpoints")
        return routes
