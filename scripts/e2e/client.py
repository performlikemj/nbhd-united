"""Authenticated, tenant-gated HTTP client for the NBHD real-flow harness."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import keychain
import requests

PRIMARY_BASE_URL = "https://api.hoodunited.org"
LEGACY_BASE_URL = "https://nbhd-django-westus2.victoriousocean-5cdd2683.westus2.azurecontainerapps.io"
DEVELOPMENT_BASE_URL = "http://localhost:8000"
ALLOWED_PRODUCTION_BASE_URLS = frozenset({PRIMARY_BASE_URL, LEGACY_BASE_URL})
ALLOWED_TENANTS_PATH = Path(__file__).with_name("allowed-tenants.json")

ACCESS_LIFETIME_MINUTES = 15
REFRESH_LIFETIME_DAYS = 60
POLL_DEADLINE_SECONDS = 900.0


class HarnessError(RuntimeError):
    """A content-free harness failure suitable for terminal output."""


class AuthenticationError(HarnessError):
    pass


class TenantGateError(HarnessError):
    pass


class HTTPStatusError(HarnessError):
    def __init__(self, method: str, path: str, status_code: int):
        super().__init__(f"HTTP {status_code} method={method} path={path}")
        self.status_code = status_code


@dataclass(frozen=True)
class MessageObservation:
    client_msg_id: str
    status: str
    source: str
    error: str
    created_at: str | None
    replied_at: str | None
    retried_at: str | None
    waking_at: str | None
    phase_present: bool
    reply_nonempty: bool
    user_redactions: tuple[dict[str, Any], ...]
    reply_redaction_count: int
    receipt_present: bool
    redaction_confirmed: bool | None
    redaction_reason: str | None

    def metadata(self) -> dict[str, Any]:
        return {
            "client_msg_id": self.client_msg_id,
            "status": self.status,
            "source": self.source,
            "error": self.error,
            "created_at": self.created_at,
            "replied_at": self.replied_at,
            "retried_at": self.retried_at,
            "waking_at": self.waking_at,
            "phase_present": self.phase_present,
            "reply_nonempty": self.reply_nonempty,
            "user_redaction_count": len(self.user_redactions),
            "reply_redaction_count": self.reply_redaction_count,
            "receipt_present": self.receipt_present,
            "redaction_confirmed": self.redaction_confirmed,
            "redaction_reason": self.redaction_reason,
        }


def poll_interval(elapsed_seconds: float) -> float:
    """Mirror iOS: 1.5s to 180s, 5s to 300s, then 15s to 900s."""
    if elapsed_seconds < 180:
        return 1.5
    if elapsed_seconds < 300:
        return 5.0
    return 15.0


def _retry_after_seconds(value: str | None, *, now: Callable[[], datetime] | None = None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            current = now() if now else datetime.now(UTC)
            return max(0.0, (when - current).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _validate_base_url(base_url: str, *, development: bool) -> str:
    normalized = base_url.rstrip("/")
    if normalized in ALLOWED_PRODUCTION_BASE_URLS:
        return normalized
    if development and normalized == DEVELOPMENT_BASE_URL:
        return normalized
    raise HarnessError("refusing non-allowlisted API base URL")


def _validate_path(path: str) -> str:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/api/v1/"):
        raise HarnessError("refusing absolute or non-API path")
    return path


def _load_allowed_tenant(path: Path = ALLOWED_TENANTS_PATH) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TenantGateError("allowed tenant configuration is unreadable") from exc
    tenant_id = payload.get("tenant_id") if isinstance(payload, dict) else None
    if not isinstance(tenant_id, str) or not tenant_id or tenant_id == "REPLACE-AFTER-PROVISION":
        raise TenantGateError("allowed tenant is not provisioned")
    return tenant_id


class NBHDClient:
    """JWT client with one 401 refresh/retry and a mandatory tenant gate."""

    def __init__(
        self,
        *,
        base_url: str = PRIMARY_BASE_URL,
        development: bool = False,
        session: requests.Session | None = None,
        allowed_tenants_path: Path = ALLOWED_TENANTS_PATH,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = _validate_base_url(base_url, development=development)
        self.session = session or requests.Session()
        self.allowed_tenants_path = allowed_tenants_path
        self.sleep = sleep
        self.monotonic = monotonic
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.tenant_profile: dict[str, Any] | None = None

    def login(self, email: str, password: str) -> dict[str, Any]:
        if not email.strip() or not password:
            raise AuthenticationError("email and password are required")
        response = self._send_raw(
            "POST",
            "/api/v1/auth/login/",
            json_body={"email": email.strip(), "password": password},
            token=None,
            timeout=30,
        )
        payload = self._json_object(response, "login")
        access = payload.get("access")
        refresh = payload.get("refresh")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise AuthenticationError("login response did not contain tokens")
        self.access_token, self.refresh_token = access, refresh
        profile = self._tenant_gate(access)
        try:
            keychain.write_tokens(access, refresh)
        except keychain.KeychainError as exc:
            raise AuthenticationError("could not store credentials in Keychain") from exc
        return profile

    def authenticate(self) -> dict[str, Any]:
        try:
            self.access_token, self.refresh_token = keychain.read_tokens()
        except keychain.KeychainError as exc:
            raise AuthenticationError("credentials unavailable; run login") from exc
        try:
            return self._tenant_gate(self.access_token)
        except HTTPStatusError as exc:
            if exc.status_code != 401:
                raise
        self._refresh_and_gate()
        if self.tenant_profile is None:
            raise AuthenticationError("refresh did not establish a tenant profile")
        return self.tenant_profile

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        if not self.access_token:
            raise AuthenticationError("client is not authenticated")
        response = self._send_raw(
            method,
            path,
            json_body=json_body,
            params=params,
            token=self.access_token,
            timeout=timeout,
            accepted_statuses={200, 201, 204, 401},
        )
        if response.status_code == 401:
            self._refresh_and_gate()
            response = self._send_raw(
                method,
                path,
                json_body=json_body,
                params=params,
                token=self.access_token,
                timeout=timeout,
                accepted_statuses={200, 201, 204},
            )
        if response.status_code == 204:
            return {}
        return self._json_object(response, path)

    def create_thread(self, title: str) -> str:
        payload = self.request("POST", "/api/v1/chat/threads/", json_body={"title": title})
        thread_id = payload.get("id")
        if not isinstance(thread_id, str) or not thread_id or payload.get("is_main") is not False:
            raise HarnessError("server did not create a disposable non-main thread")
        return thread_id

    def delete_thread(self, thread_id: str) -> None:
        self.request("DELETE", f"/api/v1/chat/threads/{thread_id}/")

    def send_message(self, *, text: str, client_msg_id: str, thread_id: str) -> MessageObservation:
        payload = self.request(
            "POST",
            "/api/v1/chat/messages/",
            json_body={"text": text, "client_msg_id": client_msg_id, "thread_id": thread_id},
            timeout=190,
        )
        return observe_message(payload, expected_client_msg_id=client_msg_id)

    def message_detail(self, client_msg_id: str) -> MessageObservation:
        payload = self.request("GET", f"/api/v1/chat/messages/{client_msg_id}/")
        return observe_message(payload, expected_client_msg_id=client_msg_id)

    def wait_for_message(
        self, client_msg_id: str, *, deadline_seconds: float = POLL_DEADLINE_SECONDS
    ) -> MessageObservation:
        started = self.monotonic()
        while True:
            observation = self.message_detail(client_msg_id)
            if observation.status in {"ready", "error"}:
                return observation
            elapsed = self.monotonic() - started
            if elapsed >= deadline_seconds:
                raise HarnessError("message polling deadline exceeded")
            self.sleep(min(poll_interval(elapsed), deadline_seconds - elapsed))

    def history(self, *, since: str | None = None, limit: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if since:
            params["since"] = since
        payload = self.request("GET", "/api/v1/chat/messages/", params=params)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise HarnessError("history response missing messages")
        return {
            "count": len(messages),
            "cursor_present": bool(payload.get("cursor")),
            "messages": [message_metadata(row) for row in messages if isinstance(row, dict)],
        }

    def entity_registry(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/api/v1/tenants/settings/entity-registry/")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise HarnessError("entity registry response missing entries")
        return [entry for entry in entries if isinstance(entry, dict)]

    def keep_pii(self, placeholders: list[str]) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/tenants/settings/pii-review-queue/keep/",
            json_body={"placeholders": placeholders},
        )

    def stop_pii(self, name: str) -> dict[str, Any]:
        return self.request("POST", "/api/v1/tenants/settings/pii-denylist/", json_body={"name": name})

    def pii_denylist(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/api/v1/tenants/settings/pii-denylist/")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise HarnessError("PII denylist response missing entries")
        return [entry for entry in entries if isinstance(entry, dict)]

    def _tenant_gate(self, access_token: str) -> dict[str, Any]:
        expected_tenant_id = _load_allowed_tenant(self.allowed_tenants_path)
        response = self._send_raw(
            "GET",
            "/api/v1/tenants/me/",
            token=access_token,
            timeout=30,
            accepted_statuses={200},
        )
        profile = self._json_object(response, "tenant gate")
        tenant = profile.get("tenant")
        actual_tenant_id = tenant.get("id") if isinstance(tenant, dict) else None
        if not isinstance(actual_tenant_id, str) or actual_tenant_id != expected_tenant_id:
            self.access_token = None
            self.refresh_token = None
            self.tenant_profile = None
            raise TenantGateError("authenticated tenant is not allowlisted")
        self.tenant_profile = profile
        return profile

    def _refresh_and_gate(self) -> None:
        if not self.refresh_token:
            raise AuthenticationError("refresh credential unavailable; run login")
        response = self._send_raw(
            "POST",
            "/api/v1/auth/refresh/",
            json_body={"refresh": self.refresh_token},
            token=None,
            timeout=30,
            accepted_statuses={200},
        )
        payload = self._json_object(response, "refresh")
        access = payload.get("access")
        if not isinstance(access, str) or not access:
            raise AuthenticationError("refresh response did not contain an access token")
        self.access_token = access
        self._tenant_gate(access)
        try:
            keychain.write_secret(keychain.ACCESS_ACCOUNT, access)
        except keychain.KeychainError as exc:
            raise AuthenticationError("could not store refreshed access credential") from exc

    def _send_raw(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        token: str | None,
        timeout: float,
        accepted_statuses: set[int] | None = None,
    ) -> requests.Response:
        method = method.upper()
        path = _validate_path(path)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        accepted = accepted_statuses or {200, 201, 204}
        for attempt in range(2):
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                raise HarnessError(f"network request failed method={method} path={path}") from exc
            if response.status_code == 429 and attempt == 0:
                delay = _retry_after_seconds(response.headers.get("Retry-After"))
                if delay is not None:
                    self.sleep(delay)
                    continue
            if response.status_code not in accepted:
                raise HTTPStatusError(method, path, response.status_code)
            return response
        raise HTTPStatusError(method, path, 429)

    @staticmethod
    def _json_object(response: requests.Response, context: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            raise HarnessError(f"invalid JSON response context={context}") from exc
        if not isinstance(payload, dict):
            raise HarnessError(f"unexpected JSON response context={context}")
        return payload


def observe_message(payload: dict[str, Any], *, expected_client_msg_id: str) -> MessageObservation:
    """Extract only metadata plus in-memory assertions; discard all text immediately."""
    client_msg_id = payload.get("client_msg_id")
    if client_msg_id != expected_client_msg_id:
        raise HarnessError("message response id mismatch")
    reply_text = payload.get("reply_text")
    reply_nonempty = isinstance(reply_text, str) and bool(reply_text.strip())
    user_redactions = payload.get("user_redactions")
    reply_redactions = payload.get("reply_redactions")
    receipt_present = "redaction_confirmed" in payload
    return MessageObservation(
        client_msg_id=client_msg_id,
        status=str(payload.get("status") or ""),
        source=str(payload.get("source") or ""),
        error=str(payload.get("error") or ""),
        created_at=payload.get("created_at") if isinstance(payload.get("created_at"), str) else None,
        replied_at=payload.get("replied_at") if isinstance(payload.get("replied_at"), str) else None,
        retried_at=payload.get("retried_at") if isinstance(payload.get("retried_at"), str) else None,
        waking_at=payload.get("waking_at") if isinstance(payload.get("waking_at"), str) else None,
        phase_present=bool(payload.get("phase")),
        reply_nonempty=reply_nonempty,
        user_redactions=tuple(row for row in user_redactions or [] if isinstance(row, dict)),
        reply_redaction_count=len(reply_redactions) if isinstance(reply_redactions, list) else 0,
        receipt_present=receipt_present,
        redaction_confirmed=(payload.get("redaction_confirmed") if receipt_present else None),
        redaction_reason=(
            payload.get("redaction_reason") if isinstance(payload.get("redaction_reason"), str) else None
        ),
    )


def message_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    redactions = payload.get("user_redactions")
    return {
        "client_msg_id": payload.get("client_msg_id"),
        "status": payload.get("status"),
        "source": payload.get("source"),
        "error": payload.get("error"),
        "created_at": payload.get("created_at"),
        "replied_at": payload.get("replied_at"),
        "retried_at": payload.get("retried_at"),
        "waking_at": payload.get("waking_at"),
        "phase_present": bool(payload.get("phase")),
        "user_redaction_count": len(redactions) if isinstance(redactions, list) else 0,
    }
