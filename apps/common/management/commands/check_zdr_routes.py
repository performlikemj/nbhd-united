"""Verify model endpoint ZDR eligibility after deploys or before model changes.

Verified 2026-08-26: OpenRouter STT ignores the request ``provider`` object, so
every STT endpoint must be ZDR; embeddings honor ``provider.zdr`` as a hard
filter, so they need at least one ZDR endpoint. Run this post-deploy and before
changing either model ID.
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

    help = "Verify the STT all-ZDR and embeddings has-ZDR-endpoint invariants."

    def handle(self, *args, **options):
        del args, options
        key = str(getattr(settings, "OPENROUTER_API_KEY", "") or "").strip()
        if not key:
            raise CommandError("OPENROUTER_API_KEY is not configured")

        stt_model = str(getattr(settings, "OPENROUTER_STT_MODEL", _DEFAULT_STT_MODEL) or _DEFAULT_STT_MODEL).strip()
        headers = {"Authorization": f"Bearer {key}"}

        try:
            stt_providers = self._fetch_model_providers(stt_model, headers)
            embedding_providers = self._fetch_model_providers(EMBEDDING_MODEL, headers)
            zdr_routes = self._fetch_zdr_routes(headers)
        except (requests.RequestException, ValueError, TypeError, KeyError):
            raise CommandError("Unable to verify OpenRouter ZDR routes") from None

        stt_zdr, stt_non_zdr = self._partition_providers(stt_model, stt_providers, zdr_routes)
        embedding_zdr, embedding_non_zdr = self._partition_providers(
            EMBEDDING_MODEL,
            embedding_providers,
            zdr_routes,
        )

        self._print_model_result(stt_model, "all_endpoints_zdr", stt_zdr, stt_non_zdr)
        self._print_model_result(
            EMBEDDING_MODEL,
            "at_least_one_endpoint_zdr",
            embedding_zdr,
            embedding_non_zdr,
        )

        failures = []
        if stt_non_zdr:
            failures.append(f"{stt_model} has non-ZDR STT endpoints")
        if not embedding_zdr:
            failures.append(f"{EMBEDDING_MODEL} has no ZDR embedding endpoint")
        if failures:
            raise CommandError("; ".join(failures))

        self.stdout.write(self.style.SUCCESS("ZDR route rules verified for 2 model(s)."))

    @staticmethod
    def _partition_providers(
        model: str,
        providers: set[str],
        zdr_routes: set[tuple[str, str]],
    ) -> tuple[list[str], list[str]]:
        zdr = sorted(provider for provider in providers if (model, provider) in zdr_routes)
        non_zdr = sorted(providers.difference(zdr))
        return zdr, non_zdr

    def _print_model_result(self, model: str, rule: str, zdr: list[str], non_zdr: list[str]) -> None:
        zdr_names = ", ".join(zdr) if zdr else "none"
        non_zdr_names = ", ".join(non_zdr) if non_zdr else "none"
        self.stdout.write(f"{model} rule={rule} zdr=[{zdr_names}] non_zdr=[{non_zdr_names}]")

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
