"""Small real-call smoke checks for dependencies used by the deployed app."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_S = 60.0
PER_CHECK_TIMEOUT_S = 15.0
SMOKE_SHARE_NAME = "ws-smoke-deploy"


class SmokeSkipped(Exception):
    """Signal that a dependency is deliberately unavailable in this environment."""


@dataclass(frozen=True)
class SmokeCheck:
    check: Callable[[], None]
    timeout_s: float = PER_CHECK_TIMEOUT_S


@dataclass(frozen=True)
class SmokeCheckResult:
    name: str
    ok: bool
    ms: int
    skipped_reason: str | None = None
    error_type: str | None = None
    error_msg: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SmokeReport:
    ok: bool
    build: str
    checks: list[SmokeCheckResult]
    total_ms: int

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "build": self.build,
            "checks": [check.as_dict() for check in self.checks],
            "total_ms": self.total_ms,
        }


def _configured_secret_values() -> list[str]:
    names = (
        "DEPLOY_SECRET",
        "GEMINI_API_KEY",
        "STRIPE_LIVE_SECRET_KEY",
        "STRIPE_TEST_SECRET_KEY",
        "QSTASH_TOKEN",
        "DATABASE_URL",
        "REDIS_URL",
    )
    return [str(value) for name in names if len(value := str(getattr(settings, name, "") or "")) >= 4]


def _sanitize_error(exc: BaseException) -> str:
    """Return one short, credential-free line and never include response bodies."""
    response = getattr(exc, "response", None)
    body_attrs = ("body", "json_body", "response_json")
    if response is not None or any(getattr(exc, attr, None) is not None for attr in body_attrs):
        status = getattr(exc, "http_status", None) or getattr(response, "status_code", None)
        return f"upstream request failed (HTTP {status})" if status else "upstream request failed"

    message = str(exc).splitlines()[0].strip() or "check failed"
    for secret in _configured_secret_values():
        message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", message)
    message = re.sub(r"\bsk_(?:live|test)_[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = re.sub(r"(https?://[^:/\s]+:)[^@\s]+@", r"\1[REDACTED]@", message)
    return message[:200]


def _require_setting(name: str) -> str:
    value = str(getattr(settings, name, "") or "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def _check_azure_storage_keys() -> None:
    from apps.orchestrator.azure_client import _is_mock, get_storage_client

    if _is_mock():
        raise SmokeSkipped("AZURE_MOCK=true")
    account = _require_setting("AZURE_STORAGE_ACCOUNT_NAME")
    client = get_storage_client()
    keys = client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account,
        connection_timeout=3,
        read_timeout=10,
        retry_total=0,
    )
    # This exact access is used by file-share read/write paths throughout the app.
    if not keys.keys[0].value:
        raise RuntimeError("Azure Storage returned an empty primary key")


def _check_azure_file_share_rw() -> None:
    from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
    from azure.storage.fileshare import ShareDirectoryClient, ShareFileClient

    from apps.orchestrator.azure_client import _is_mock, get_storage_client

    if _is_mock():
        raise SmokeSkipped("AZURE_MOCK=true")

    account = _require_setting("AZURE_STORAGE_ACCOUNT_NAME")
    storage_client = get_storage_client()
    # Same management-client creation pattern as create_tenant_file_share, but
    # permanently isolated from every tenant share by this fixed dedicated name.
    request_options = {
        "connection_timeout": 3,
        "read_timeout": 10,
        "retry_total": 0,
    }
    try:
        storage_client.file_shares.get(
            resource_group_name=settings.AZURE_RESOURCE_GROUP,
            account_name=account,
            share_name=SMOKE_SHARE_NAME,
            **request_options,
        )
    except ResourceNotFoundError:
        storage_client.file_shares.create(
            resource_group_name=settings.AZURE_RESOURCE_GROUP,
            account_name=account,
            share_name=SMOKE_SHARE_NAME,
            file_share={},
            **request_options,
        )
    keys = storage_client.storage_accounts.list_keys(
        settings.AZURE_RESOURCE_GROUP,
        account,
        connection_timeout=3,
        read_timeout=10,
        retry_total=0,
    )
    account_key = keys.keys[0].value
    if not account_key:
        raise RuntimeError("Azure Storage returned an empty primary key")

    account_url = f"https://{account}.file.core.windows.net"
    for directory_path in ("workspace", "workspace/smoke"):
        directory = ShareDirectoryClient(
            account_url=account_url,
            share_name=SMOKE_SHARE_NAME,
            directory_path=directory_path,
            credential=account_key,
            **request_options,
        )
        try:
            directory.create_directory(timeout=10)
        except ResourceExistsError:
            pass

    file_path = f"workspace/smoke/{uuid.uuid4()}.txt"
    file_client = ShareFileClient(
        account_url=account_url,
        share_name=SMOKE_SHARE_NAME,
        file_path=file_path,
        credential=account_key,
        **request_options,
    )
    created = False
    try:
        file_client.upload_file(b"1", length=1, timeout=10)
        created = True
        if file_client.download_file(timeout=10).readall() != b"1":
            raise RuntimeError("Azure File Share read-back did not match the byte written")
    finally:
        # The UUID path was minted by this invocation; no pre-existing or tenant
        # object is ever considered for deletion.
        if created:
            file_client.delete_file(timeout=10)


def _check_azure_keyvault() -> None:
    from azure.keyvault.secrets import SecretClient

    from apps.orchestrator.azure_client import _get_provisioner_credential, _is_mock

    if _is_mock():
        raise SmokeSkipped("AZURE_MOCK=true")
    vault_name = _require_setting("AZURE_KEY_VAULT_NAME")
    # OpenClaw containers consume this Key Vault-backed secret at startup.
    secret_name = _require_setting("AZURE_KV_SECRET_NBHD_INTERNAL_API_KEY")
    client = SecretClient(
        vault_url=f"https://{vault_name}.vault.azure.net",
        credential=_get_provisioner_credential(),
        connection_timeout=3,
        read_timeout=10,
        retry_total=0,
    )
    secret = client.get_secret(
        secret_name,
        connection_timeout=3,
        read_timeout=10,
        retry_total=0,
    )
    if not secret.properties.version:
        raise RuntimeError("Key Vault secret metadata has no version")


def _check_gemini_tts() -> None:
    from apps.core import render
    from apps.orchestrator.azure_client import _is_mock

    api_key = str(getattr(settings, "GEMINI_API_KEY", "") or "").strip()
    if _is_mock():
        raise SmokeSkipped("AZURE_MOCK=true")
    if not api_key:
        raise SmokeSkipped("GEMINI_API_KEY is not configured")

    from google.genai import types

    # Gemini rejects manually set deadlines below 10s. Two worst-case requests
    # plus the 2s retry backoff fit within this check's 25s runner deadline.
    client = render.make_gemini_client(api_key, timeout_ms=10_000)
    text = "Take a slow breath in, and let it go gently."
    prompt = (
        "Read the following aloud in a soft, calm, slow, soothing "
        "meditation-guide voice. Be concise. Do not read these instructions aloud.\n\n"
        f"{text}"
    )
    attempts = 2
    last_error = "no audio bytes"
    for attempt in range(attempts):
        try:
            response = client.models.generate_content(
                model=getattr(settings, "GEMINI_TTS_MODEL", "") or render.DEFAULT_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=render.DEFAULT_VOICE)
                        )
                    ),
                ),
            )
            if render._extract_audio(response):
                return
            last_error = "no audio bytes"
        except Exception as exc:  # noqa: BLE001 - mirror production's transient retry behavior
            last_error = str(exc)[:200]
            if render._is_rate_limit(last_error):
                raise SmokeSkipped("gemini rate-limited") from exc
            normalized_error = last_error.lower()
            if "400" in normalized_error and "invalid_argument" in normalized_error and "deadline" in normalized_error:
                raise

        if attempt + 1 < attempts:
            time.sleep(min(2 ** (attempt + 1), 4))

    raise RuntimeError(f"Gemini TTS failed after {attempts} attempts: {last_error}")


def _check_stripe() -> None:
    import stripe

    key_name = "STRIPE_LIVE_SECRET_KEY" if settings.STRIPE_LIVE_MODE else "STRIPE_TEST_SECRET_KEY"
    api_key = _require_setting(key_name)
    stripe.Balance.retrieve(api_key=api_key)


def _check_qstash() -> None:
    from apps.cron.publish import _get_qstash_client

    token = _require_setting("QSTASH_TOKEN")
    _get_qstash_client(token).schedule.list()


def _check_db() -> None:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        if cursor.fetchone() != (1,):
            raise RuntimeError("SELECT 1 returned an unexpected result")


def _check_cache() -> None:
    from django.core.cache import cache

    key = f"smoke-external-deps:{uuid.uuid4()}"
    value = uuid.uuid4().hex
    try:
        cache.set(key, value, timeout=30)
        if cache.get(key) != value:
            raise RuntimeError("cache read-back did not match")
    finally:
        cache.delete(key)


_CHECKS: dict[str, SmokeCheck] = {
    "azure_storage_keys": SmokeCheck(_check_azure_storage_keys),
    "azure_file_share_rw": SmokeCheck(_check_azure_file_share_rw),
    "azure_keyvault": SmokeCheck(_check_azure_keyvault),
    "gemini_tts": SmokeCheck(_check_gemini_tts, timeout_s=25.0),
    "stripe": SmokeCheck(_check_stripe),
    "qstash": SmokeCheck(_check_qstash),
    "db": SmokeCheck(_check_db),
    "cache": SmokeCheck(_check_cache),
}


def _execute_check(name: str, check: Callable[[], None]) -> SmokeCheckResult:
    started = time.monotonic()
    try:
        check()
    except SmokeSkipped as exc:
        return SmokeCheckResult(
            name=name,
            ok=True,
            ms=round((time.monotonic() - started) * 1000),
            skipped_reason=_sanitize_error(exc),
        )
    except Exception as exc:  # noqa: BLE001 - every check must report, never abort aggregation
        return SmokeCheckResult(
            name=name,
            ok=False,
            ms=round((time.monotonic() - started) * 1000),
            error_type=type(exc).__name__,
            error_msg=_sanitize_error(exc),
        )
    return SmokeCheckResult(name=name, ok=True, ms=round((time.monotonic() - started) * 1000))


def run_smoke(checks: list[str] | None = None, *, budget_s: float = DEFAULT_BUDGET_S) -> SmokeReport:
    """Run selected real dependency checks concurrently within strict deadlines."""
    started = time.monotonic()
    if budget_s <= 0:
        raise ValueError("budget_s must be positive")

    selected = list(dict.fromkeys(checks)) if checks is not None else list(_CHECKS)
    unknown = [name for name in selected if name not in _CHECKS]
    if unknown:
        raise ValueError(f"unknown smoke check(s): {', '.join(unknown)}")
    if not selected:
        return SmokeReport(
            ok=True,
            build=str(getattr(settings, "SENTRY_RELEASE", "") or ""),
            checks=[],
            total_ms=round((time.monotonic() - started) * 1000),
        )

    executor = ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="external-deps-smoke")
    submitted: dict[Future, tuple[str, float, float]] = {}
    for name in selected:
        smoke_check = _CHECKS[name]
        submitted[executor.submit(_execute_check, name, smoke_check.check)] = (
            name,
            time.monotonic(),
            smoke_check.timeout_s,
        )

    pending = set(submitted)
    by_name: dict[str, SmokeCheckResult] = {}
    overall_deadline = started + budget_s
    try:
        while pending:
            now = time.monotonic()
            next_deadline = min(
                overall_deadline,
                *(submitted[future][1] + submitted[future][2] for future in pending),
            )
            done, _ = wait(pending, timeout=max(0.0, next_deadline - now), return_when=FIRST_COMPLETED)
            for future in done:
                pending.remove(future)
                result = future.result()
                timeout_s = submitted[future][2]
                if result.ms <= round(timeout_s * 1000):
                    by_name[result.name] = result
                else:
                    by_name[result.name] = SmokeCheckResult(
                        name=result.name,
                        ok=False,
                        ms=round(timeout_s * 1000),
                        error_type="TimeoutError",
                        error_msg=f"check exceeded {timeout_s:g}s timeout",
                    )

            now = time.monotonic()
            expired = [
                future
                for future in pending
                if now >= min(overall_deadline, submitted[future][1] + submitted[future][2])
            ]
            for future in expired:
                pending.remove(future)
                name, submitted_at, check_timeout_s = submitted[future]
                future.cancel()
                elapsed_ms = round((min(now, overall_deadline) - submitted_at) * 1000)
                timeout_s = min(check_timeout_s, budget_s)
                by_name[name] = SmokeCheckResult(
                    name=name,
                    ok=False,
                    ms=max(0, elapsed_ms),
                    error_type="TimeoutError",
                    error_msg=f"check exceeded {timeout_s:g}s timeout",
                )
    finally:
        # A timed-out worker must never hold the HTTP request open. Provider
        # clients also carry their own <=15s transport timeout where supported.
        executor.shutdown(wait=False, cancel_futures=True)

    results = [by_name[name] for name in selected]
    failed_names = [result.name for result in results if not result.ok]
    if failed_names:
        logger.error("smoke_external_deps FAILED: %s", failed_names)
    return SmokeReport(
        ok=not failed_names,
        build=str(getattr(settings, "SENTRY_RELEASE", "") or ""),
        checks=results,
        total_ms=round((time.monotonic() - started) * 1000),
    )
