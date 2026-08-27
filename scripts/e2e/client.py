"""Strict production HTTP client for the NBHD real-flow E2E harness."""

from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
ALLOWED_PRODUCTION_BASE_URLS = frozenset({PRIMARY_BASE_URL, LEGACY_BASE_URL})
ALLOWED_TENANTS_PATH = Path(__file__).with_name("allowed-tenants.json")

ACCESS_LIFETIME_MINUTES = 15
REFRESH_LIFETIME_DAYS = 60
POLL_DEADLINE_SECONDS = 900.0
READ_DEADLINE_SECONDS = 60.0

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]{1,510}={0,2}$")
_HARNESS_MESSAGE_ID_RE = re.compile(r"^nbhd-e2e-(?:chat|follow-up|smoke|wake)-[0-9a-f]{32}$")
_PLACEHOLDER_RE = re.compile(r"^\[[A-Z][A-Z_]*_[1-9][0-9]*\]$")
_ENDPOINT_LABELS = frozenset(
    {
        "auth_login",
        "auth_refresh",
        "tenant_me",
        "chat_threads_create",
        "chat_threads_delete",
        "chat_messages_send",
        "chat_message_detail",
        "chat_history",
        "entity_registry",
        "pii_review_keep",
        "pii_denylist_add",
        "pii_denylist_list",
    }
)


class HarnessError(RuntimeError):
    """A content-free harness failure suitable for terminal output."""


class AuthenticationError(HarnessError):
    pass


class TenantGateError(HarnessError):
    pass


class DeadlineExceeded(HarnessError):
    pass


class HTTPStatusError(HarnessError):
    def __init__(self, method: str, endpoint: str, status_code: int):
        super().__init__(f"HTTP {status_code} method={method} endpoint={endpoint}")
        self.status_code = status_code


@dataclass(frozen=True)
class Allowlist:
    tenant_id: str
    email: str


@dataclass(frozen=True)
class RegistryEntry:
    placeholder: str
    name: str
    updated_at: str | None


@dataclass(frozen=True)
class StopPIIResult:
    reason: str
    decided_at: str
    retired_count: int


@dataclass(frozen=True)
class MessageObservation:
    client_msg_id: str
    status: str
    source: str
    error: str
    created_at: str
    replied_at: str | None
    retried_at: str | None
    waking_at: str | None
    phase_present: bool
    reply_nonempty: bool
    user_redactions: tuple[dict[str, str], ...]
    reply_redaction_count: int
    receipt_present: bool
    redaction_confirmed: bool | None
    redaction_reason: str

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
    if elapsed_seconds < 180:
        return 1.5
    if elapsed_seconds < 300:
        return 5.0
    return 15.0


def validate_harness_message_id(value: str) -> str:
    if not isinstance(value, str) or not _HARNESS_MESSAGE_ID_RE.fullmatch(value):
        raise HarnessError("invalid harness message id")
    return value


def validate_cursor(value: str) -> str:
    if not isinstance(value, str) or not _CURSOR_RE.fullmatch(value):
        raise HarnessError("invalid history cursor")
    return value


def normalize_timestamp(value: Any, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise HarnessError("server returned an invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HarnessError("server returned an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise HarnessError("server returned a timezone-free timestamp")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _closed_enum(value: Any, allowed: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise HarnessError("server returned an invalid enum")
    return value if value in allowed else "other"


def _error_state(value: Any) -> str:
    if not isinstance(value, str):
        raise HarnessError("server returned an invalid error field")
    return "none" if not value else "other"


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HarnessError(f"server returned an invalid {field} count")
    return value


def _retry_after_seconds(value: str | None, *, now: Callable[[], datetime] | None = None) -> float | None:
    if not value or len(value) > 128:
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


def _validate_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized not in ALLOWED_PRODUCTION_BASE_URLS:
        raise HarnessError("refusing non-allowlisted production API base URL")
    return normalized


def _validate_path(path: str) -> str:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/api/v1/"):
        raise HarnessError("refusing absolute or non-API path")
    return path


def _canonical_uuid4(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise HarnessError(f"invalid {field}")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HarnessError(f"invalid {field}") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise HarnessError(f"invalid {field}")
    return value


def load_allowlist(path: Path = ALLOWED_TENANTS_PATH) -> Allowlist:
    """Read an owned, non-writable regular file without following symlinks."""
    if path.is_symlink():
        raise TenantGateError("allowed tenant configuration must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TenantGateError("allowed tenant configuration is unreadable") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise TenantGateError("allowed tenant configuration must be a regular file")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise TenantGateError("allowed tenant configuration has the wrong owner")
        if file_stat.st_mode & 0o022:
            raise TenantGateError("allowed tenant configuration is writable by another principal")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            raw = handle.read(4097)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > 4096:
        raise TenantGateError("allowed tenant configuration is too large")

    def reject_duplicate_keys(pairs):
        decoded = {}
        for key, value in pairs:
            if key in decoded:
                raise ValueError("duplicate JSON key")
            decoded[key] = value
        return decoded

    try:
        payload = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise TenantGateError("allowed tenant configuration is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"tenant_id", "email"}:
        raise TenantGateError("allowed tenant configuration has an invalid schema")
    tenant_id = payload.get("tenant_id")
    email = payload.get("email")
    if tenant_id == "REPLACE-AFTER-PROVISION" or email == "REPLACE-AFTER-PROVISION":
        raise TenantGateError("allowed tenant is not provisioned")
    try:
        tenant_id = _canonical_uuid4(tenant_id, field="allowlisted tenant id")
    except HarnessError as exc:
        raise TenantGateError("allowed tenant id must be a canonical UUID4") from exc
    if not isinstance(email, str) or email != email.strip() or not _EMAIL_RE.fullmatch(email):
        raise TenantGateError("allowed tenant email is invalid")
    return Allowlist(tenant_id=tenant_id, email=email)


class NBHDClient:
    """JWT client with redirects disabled and a universal synthetic tenant gate."""

    def __init__(
        self,
        *,
        base_url: str = PRIMARY_BASE_URL,
        session: requests.Session | None = None,
        allowed_tenants_path: Path = ALLOWED_TENANTS_PATH,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self.session = session or requests.Session()
        self.allowed_tenants_path = allowed_tenants_path
        self.sleep = sleep
        self.monotonic = monotonic
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.tenant_profile: dict[str, Any] | None = None
        self.tenant_hibernated = False

    def new_deadline(self, seconds: float) -> float:
        if seconds <= 0:
            raise ValueError("deadline seconds must be positive")
        return self.monotonic() + seconds

    def login(self, password: str, *, deadline: float) -> dict[str, Any]:
        if not password:
            raise AuthenticationError("password is required")
        allowlist = load_allowlist(self.allowed_tenants_path)
        response = self._send_raw(
            "POST",
            "/api/v1/auth/login/",
            endpoint="auth_login",
            json_body={"email": allowlist.email, "password": password},
            token=None,
            timeout_cap=30,
            deadline=deadline,
        )
        payload = self._json_object(response, "auth_login")
        access = payload.get("access")
        refresh = payload.get("refresh")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise AuthenticationError("login response did not contain valid credentials")
        self.access_token, self.refresh_token = access, refresh
        profile = self._tenant_gate(access, deadline=deadline)
        try:
            keychain.write_credentials(keychain.Credentials(access=access, refresh=refresh))
        except keychain.KeychainError as exc:
            raise AuthenticationError("could not store credentials in Keychain") from exc
        return profile

    def authenticate(self, *, deadline: float) -> dict[str, Any]:
        try:
            credentials = keychain.read_credentials()
        except keychain.KeychainError as exc:
            raise AuthenticationError("credentials unavailable; run login") from exc
        self.access_token, self.refresh_token = credentials.access, credentials.refresh
        try:
            return self._tenant_gate(self.access_token, deadline=deadline)
        except HTTPStatusError as exc:
            if exc.status_code != 401:
                raise
        self._refresh_and_gate(deadline=deadline)
        if self.tenant_profile is None:
            raise AuthenticationError("refresh did not establish a tenant profile")
        return self.tenant_profile

    def request(
        self,
        method: str,
        path: str,
        *,
        endpoint: str,
        deadline: float,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_cap: float = 30,
    ) -> dict[str, Any]:
        if not self.access_token:
            raise AuthenticationError("client is not authenticated")
        response = self._send_raw(
            method,
            path,
            endpoint=endpoint,
            json_body=json_body,
            params=params,
            token=self.access_token,
            timeout_cap=timeout_cap,
            deadline=deadline,
            accepted_statuses={200, 201, 204, 401},
        )
        if response.status_code == 401:
            self._refresh_and_gate(deadline=deadline)
            response = self._send_raw(
                method,
                path,
                endpoint=endpoint,
                json_body=json_body,
                params=params,
                token=self.access_token,
                timeout_cap=timeout_cap,
                deadline=deadline,
                accepted_statuses={200, 201, 204},
            )
        if response.status_code == 204:
            return {}
        return self._json_object(response, endpoint)

    @contextmanager
    def managed_thread(
        self,
        title: str,
        *,
        deadline: float,
        cleanup_reporter: Callable[[], None],
    ) -> Iterator[str]:
        """Create, validate, and always attempt to delete one non-main thread."""
        thread_id: str | None = None
        primary_error: BaseException | None = None
        try:
            payload = self.request(
                "POST",
                "/api/v1/chat/threads/",
                endpoint="chat_threads_create",
                json_body={"title": title},
                deadline=deadline,
            )
            raw_thread_id = payload.get("id")
            thread_id = _canonical_uuid4(raw_thread_id, field="thread id")
            if payload.get("is_main") is not False:
                raise HarnessError("server did not create a disposable non-main thread")
            yield thread_id
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if thread_id is not None:
                try:
                    self.delete_thread(thread_id, deadline=deadline)
                except HarnessError:
                    cleanup_reporter()
                    if primary_error is None:
                        raise

    def delete_thread(self, thread_id: str, *, deadline: float) -> None:
        thread_id = _canonical_uuid4(thread_id, field="thread id")
        self.request(
            "DELETE",
            f"/api/v1/chat/threads/{thread_id}/",
            endpoint="chat_threads_delete",
            deadline=deadline,
        )

    def send_message(
        self,
        *,
        text: str,
        client_msg_id: str,
        thread_id: str,
        deadline: float,
    ) -> MessageObservation:
        client_msg_id = validate_harness_message_id(client_msg_id)
        thread_id = _canonical_uuid4(thread_id, field="thread id")
        payload = self.request(
            "POST",
            "/api/v1/chat/messages/",
            endpoint="chat_messages_send",
            json_body={"text": text, "client_msg_id": client_msg_id, "thread_id": thread_id},
            timeout_cap=190,
            deadline=deadline,
        )
        return observe_message(payload, expected_client_msg_id=client_msg_id)

    def message_detail(self, client_msg_id: str, *, deadline: float) -> MessageObservation:
        client_msg_id = validate_harness_message_id(client_msg_id)
        payload = self.request(
            "GET",
            f"/api/v1/chat/messages/{client_msg_id}/",
            endpoint="chat_message_detail",
            deadline=deadline,
        )
        return observe_message(payload, expected_client_msg_id=client_msg_id)

    def wait_for_message(self, client_msg_id: str, *, deadline: float) -> MessageObservation:
        client_msg_id = validate_harness_message_id(client_msg_id)
        started = self.monotonic()
        while True:
            observation = self.message_detail(client_msg_id, deadline=deadline)
            if observation.status in {"ready", "error"}:
                return observation
            remaining = self._remaining(deadline)
            elapsed = self.monotonic() - started
            self.sleep(min(poll_interval(elapsed), remaining))

    def history(self, *, deadline: float, since: str | None = None, limit: int = 50) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise HarnessError("history limit must be between 1 and 100")
        params: dict[str, Any] = {"limit": limit}
        if since is not None:
            params["since"] = validate_cursor(since)
        payload = self.request(
            "GET",
            "/api/v1/chat/messages/",
            endpoint="chat_history",
            params=params,
            deadline=deadline,
        )
        messages = payload.get("messages")
        cursor = payload.get("cursor")
        if not isinstance(messages, list):
            raise HarnessError("history response missing messages")
        if cursor is not None:
            validate_cursor(cursor)
        source_counts: Counter[str] = Counter()
        for row in messages:
            if not isinstance(row, dict):
                raise HarnessError("history response contains an invalid row")
            source = _closed_enum(row.get("source"), frozenset({"app", "telegram", "line", "cron"}))
            normalize_timestamp(row.get("created_at"), nullable=False)
            source_counts[source] += 1
        return {
            "count": len(messages),
            "cursor_present": cursor is not None,
            "source_counts": dict(sorted(source_counts.items())),
        }

    def entity_registry(self, *, deadline: float) -> list[RegistryEntry]:
        payload = self.request(
            "GET",
            "/api/v1/tenants/settings/entity-registry/",
            endpoint="entity_registry",
            deadline=deadline,
        )
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise HarnessError("entity registry response missing entries")
        validated = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise HarnessError("entity registry contains an invalid entry")
            placeholder = entry.get("placeholder")
            name = entry.get("name")
            if not isinstance(placeholder, str) or not _PLACEHOLDER_RE.fullmatch(placeholder):
                raise HarnessError("entity registry contains an invalid placeholder")
            if not isinstance(name, str) or len(name) > 256:
                raise HarnessError("entity registry contains an invalid name")
            for optional_text in ("relationship", "notes"):
                if optional_text in entry and not isinstance(entry[optional_text], str):
                    raise HarnessError("entity registry contains invalid metadata")
            updated_at = normalize_timestamp(entry.get("updated_at"), nullable=True)
            validated.append(RegistryEntry(placeholder=placeholder, name=name, updated_at=updated_at))
        return validated

    def keep_fixture_pii(self, *, deadline: float) -> dict[str, int | str]:
        import fixtures

        entries = self.entity_registry(deadline=deadline)
        placeholders = sorted(
            entry.placeholder for entry in entries if entry.name in {fixtures.PERSON_NAME, fixtures.PHONE_NUMBER}
        )
        if not placeholders:
            raise HarnessError("no current fixture PII bindings are available to keep")
        payload = self.request(
            "POST",
            "/api/v1/tenants/settings/pii-review-queue/keep/",
            endpoint="pii_review_keep",
            json_body={"placeholders": placeholders},
            deadline=deadline,
        )
        kept = payload.get("kept")
        not_found = payload.get("not_found")
        if not isinstance(kept, list) or not isinstance(not_found, list):
            raise HarnessError("PII keep response has an invalid shape")
        if any(value not in placeholders for value in kept + not_found):
            raise HarnessError("PII keep response referenced a non-fixture binding")
        return {"status": "complete", "kept_count": len(kept), "not_found_count": len(not_found)}

    def stop_pii(self, name: str, *, deadline: float) -> StopPIIResult:
        import fixtures

        if name not in {fixtures.PERSON_NAME, fixtures.PHONE_NUMBER}:
            raise HarnessError("pii stop accepts fixed fixture values only")
        payload = self.request(
            "POST",
            "/api/v1/tenants/settings/pii-denylist/",
            endpoint="pii_denylist_add",
            json_body={"name": name},
            deadline=deadline,
        )
        if payload.get("key") != name.casefold():
            raise HarnessError("PII stop response returned an unexpected canonical key")
        reason = _closed_enum(payload.get("reason"), frozenset({"manual"}))
        decided_at = normalize_timestamp(payload.get("decided_at"), nullable=False)
        retired_count = _nonnegative_int(payload.get("retired"), "retired")
        return StopPIIResult(reason=reason, decided_at=decided_at, retired_count=retired_count)

    def pii_denylist_contains(self, name: str, *, deadline: float) -> bool:
        payload = self.request(
            "GET",
            "/api/v1/tenants/settings/pii-denylist/",
            endpoint="pii_denylist_list",
            deadline=deadline,
        )
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise HarnessError("PII denylist response missing entries")
        expected = name.casefold()
        found = False
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
                raise HarnessError("PII denylist contains an invalid entry")
            _closed_enum(entry.get("reason"), frozenset({"manual"}))
            normalize_timestamp(entry.get("decided_at"), nullable=False)
            if entry["key"] == expected:
                found = True
        return found

    def _tenant_gate(self, access_token: str, *, deadline: float) -> dict[str, Any]:
        allowlist = load_allowlist(self.allowed_tenants_path)
        response = self._send_raw(
            "GET",
            "/api/v1/tenants/me/",
            endpoint="tenant_me",
            token=access_token,
            timeout_cap=30,
            deadline=deadline,
            accepted_statuses={200},
        )
        profile = self._json_object(response, "tenant_me")
        if "is_synthetic" not in profile or "is_eval_sink" not in profile:
            self._clear_auth()
            raise TenantGateError(
                "tenant safety gate is missing is_synthetic/is_eval_sink; deploy feat/chat-redaction-receipt"
            )
        actual_tenant_id = profile.get("id")
        if actual_tenant_id != allowlist.tenant_id:
            self._clear_auth()
            raise TenantGateError("authenticated tenant is not allowlisted")
        if profile.get("is_synthetic") is not True or profile.get("is_eval_sink") is not False:
            self._clear_auth()
            raise TenantGateError("authenticated tenant is not explicitly synthetic and non-eval-sink")
        hibernated_at = normalize_timestamp(profile.get("hibernated_at"), nullable=True)
        self.tenant_profile = profile
        self.tenant_hibernated = hibernated_at is not None
        return profile

    def _refresh_and_gate(self, *, deadline: float) -> None:
        if not self.refresh_token:
            raise AuthenticationError("refresh credential unavailable; run login")
        response = self._send_raw(
            "POST",
            "/api/v1/auth/refresh/",
            endpoint="auth_refresh",
            json_body={"refresh": self.refresh_token},
            token=None,
            timeout_cap=30,
            deadline=deadline,
            accepted_statuses={200},
        )
        payload = self._json_object(response, "auth_refresh")
        access = payload.get("access")
        if not isinstance(access, str) or not access:
            raise AuthenticationError("refresh response did not contain an access credential")
        rotated_refresh = payload.get("refresh", self.refresh_token)
        if not isinstance(rotated_refresh, str) or not rotated_refresh:
            raise AuthenticationError("refresh response contained an invalid refresh credential")
        self.access_token = access
        self._tenant_gate(access, deadline=deadline)
        credentials = keychain.Credentials(access=access, refresh=rotated_refresh)
        try:
            keychain.write_credentials(credentials)
        except keychain.KeychainError as exc:
            raise AuthenticationError("could not store refreshed credentials") from exc
        self.refresh_token = rotated_refresh

    def _send_raw(
        self,
        method: str,
        path: str,
        *,
        endpoint: str,
        deadline: float,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        token: str | None,
        timeout_cap: float,
        accepted_statuses: set[int] | None = None,
    ) -> requests.Response:
        method = method.upper()
        path = _validate_path(path)
        if endpoint not in _ENDPOINT_LABELS:
            raise HarnessError("invalid endpoint label")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        accepted = accepted_statuses or {200, 201, 204}
        for attempt in range(2):
            timeout = min(timeout_cap, self._remaining(deadline))
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise HarnessError(f"network request failed method={method} endpoint={endpoint}") from exc
            self._remaining(deadline)
            if 300 <= response.status_code < 400:
                raise HTTPStatusError(method, endpoint, response.status_code)
            if response.status_code == 429 and attempt == 0:
                delay = _retry_after_seconds(response.headers.get("Retry-After"))
                remaining = self._remaining(deadline)
                if delay is None or delay >= remaining:
                    raise DeadlineExceeded("Retry-After exceeds the command deadline")
                self.sleep(delay)
                continue
            if response.status_code not in accepted:
                raise HTTPStatusError(method, endpoint, response.status_code)
            return response
        raise HTTPStatusError(method, endpoint, 429)

    def _remaining(self, deadline: float) -> float:
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise DeadlineExceeded("invalid command deadline")
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("command deadline exceeded")
        return remaining

    def _clear_auth(self) -> None:
        self.access_token = None
        self.refresh_token = None
        self.tenant_profile = None
        self.tenant_hibernated = False

    @staticmethod
    def _json_object(response: requests.Response, endpoint: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            raise HarnessError(f"invalid JSON response endpoint={endpoint}") from exc
        if not isinstance(payload, dict):
            raise HarnessError(f"unexpected JSON response endpoint={endpoint}")
        return payload


def observe_message(payload: dict[str, Any], *, expected_client_msg_id: str) -> MessageObservation:
    """Extract closed metadata while retaining fixture assertions only in memory."""
    expected_client_msg_id = validate_harness_message_id(expected_client_msg_id)
    client_msg_id = payload.get("client_msg_id")
    if client_msg_id != expected_client_msg_id:
        raise HarnessError("message response id mismatch")
    validate_harness_message_id(client_msg_id)
    reply_text = payload.get("reply_text")
    if not isinstance(reply_text, str):
        raise HarnessError("message response has an invalid reply field")
    phase = payload.get("phase")
    if phase is not None and not isinstance(phase, str):
        raise HarnessError("message response has an invalid phase")
    user_redactions = _validate_redactions(payload.get("user_redactions"), "user")
    reply_redactions = _validate_redactions(payload.get("reply_redactions"), "reply")
    receipt_present = "redaction_confirmed" in payload
    confirmed = payload.get("redaction_confirmed") if receipt_present else None
    if confirmed is not None and not isinstance(confirmed, bool):
        raise HarnessError("message response has an invalid redaction confirmation")
    reason_value = payload.get("redaction_reason")
    if reason_value is None:
        redaction_reason = "none"
    else:
        redaction_reason = _closed_enum(reason_value, frozenset({"redacted"}))
    return MessageObservation(
        client_msg_id=client_msg_id,
        status=_closed_enum(payload.get("status"), frozenset({"pending", "ready", "error"})),
        source=_closed_enum(payload.get("source"), frozenset({"tenant", "control_plane", "on_device"})),
        error=_error_state(payload.get("error")),
        created_at=normalize_timestamp(payload.get("created_at"), nullable=False),
        replied_at=normalize_timestamp(payload.get("replied_at"), nullable=True),
        retried_at=normalize_timestamp(payload.get("retried_at"), nullable=True),
        waking_at=normalize_timestamp(payload.get("waking_at"), nullable=True),
        phase_present=bool(phase),
        reply_nonempty=bool(reply_text.strip()),
        user_redactions=user_redactions,
        reply_redaction_count=len(reply_redactions),
        receipt_present=receipt_present,
        redaction_confirmed=confirmed,
        redaction_reason=redaction_reason,
    )


def _validate_redactions(value: Any, label: str) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise HarnessError(f"message response has invalid {label} redactions")
    validated = []
    for row in value:
        if not isinstance(row, dict):
            raise HarnessError(f"message response has invalid {label} redactions")
        placeholder = row.get("placeholder")
        mapping_value = row.get("value")
        if not isinstance(placeholder, str) or not _PLACEHOLDER_RE.fullmatch(placeholder):
            raise HarnessError(f"message response has invalid {label} placeholder")
        if not isinstance(mapping_value, str) or len(mapping_value) > 512:
            raise HarnessError(f"message response has invalid {label} mapping")
        validated.append({"placeholder": placeholder, "value": mapping_value})
    return tuple(validated)
