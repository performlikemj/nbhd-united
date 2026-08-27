"""Atomic macOS Keychain storage for the production E2E JWT pair."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

SERVICE = "org.nbhd.e2e"
CREDENTIALS_ACCOUNT = "credentials"
_LEGACY_ACCESS_ACCOUNT = "access"
_LEGACY_REFRESH_ACCOUNT = "refresh"
_ITEM_NOT_FOUND_RETURN_CODE = 44
_SUBPROCESS_TIMEOUT_CAP_SECONDS = 30.0


class KeychainError(RuntimeError):
    """A content-free Keychain failure."""


class MissingKeychainItem(KeychainError):
    pass


@dataclass(frozen=True)
class Credentials:
    access: str
    refresh: str


def _remaining_timeout(deadline: float, monotonic: Callable[[], float]) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise KeychainError("invalid Keychain deadline")
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise KeychainError("Keychain operation deadline exceeded")
    return min(_SUBPROCESS_TIMEOUT_CAP_SECONDS, remaining)


def _run_security(
    argv: list[str],
    *,
    deadline: float,
    monotonic: Callable[[], float],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            timeout=_remaining_timeout(deadline, monotonic),
        )
    except subprocess.TimeoutExpired as exc:
        raise KeychainError("Keychain operation timed out") from exc


def _read_secret(account: str, *, deadline: float, monotonic: Callable[[], float]) -> str:
    """Capture a secret directly into memory; never inherit terminal output."""
    result = _run_security(
        ["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
        deadline=deadline,
        monotonic=monotonic,
    )
    if result.returncode == _ITEM_NOT_FOUND_RETURN_CODE:
        raise MissingKeychainItem("Keychain credential unavailable")
    if result.returncode != 0:
        raise KeychainError("Keychain credential read failed")
    secret = result.stdout.rstrip("\r\n")
    if not secret:
        raise KeychainError("Keychain credential empty")
    return secret


def _write_secret(
    account: str,
    secret: str,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> None:
    """Write through security's stdin prompt; the secret is never an argv value."""
    if not secret or "\n" in secret or "\r" in secret:
        raise KeychainError("invalid Keychain credential payload")
    result = _run_security(
        ["security", "add-generic-password", "-U", "-a", account, "-s", SERVICE, "-w"],
        input_text=f"{secret}\n",
        deadline=deadline,
        monotonic=monotonic,
    )
    if result.returncode != 0:
        raise KeychainError("Keychain credential write failed")


def _delete_secret(account: str, *, deadline: float, monotonic: Callable[[], float]) -> None:
    result = _run_security(
        ["security", "delete-generic-password", "-a", account, "-s", SERVICE],
        deadline=deadline,
        monotonic=monotonic,
    )
    if result.returncode not in {0, _ITEM_NOT_FOUND_RETURN_CODE}:
        raise KeychainError("legacy Keychain credential deletion failed")


def _parse_credentials(payload: str) -> Credentials:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise KeychainError("Keychain credential payload is invalid") from exc
    if not isinstance(decoded, dict) or set(decoded) != {"access", "refresh"}:
        raise KeychainError("Keychain credential payload has an invalid schema")
    access = decoded.get("access")
    refresh = decoded.get("refresh")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise KeychainError("Keychain credential payload has invalid values")
    return Credentials(access=access, refresh=refresh)


def read_credentials(
    *,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> Credentials:
    """Read the atomic item, migrating the former two-item layout once."""
    try:
        return _parse_credentials(_read_secret(CREDENTIALS_ACCOUNT, deadline=deadline, monotonic=monotonic))
    except MissingKeychainItem as atomic_error:
        try:
            legacy_access = _read_secret(_LEGACY_ACCESS_ACCOUNT, deadline=deadline, monotonic=monotonic)
            legacy_refresh = _read_secret(_LEGACY_REFRESH_ACCOUNT, deadline=deadline, monotonic=monotonic)
        except MissingKeychainItem:
            raise atomic_error
        legacy = Credentials(access=legacy_access, refresh=legacy_refresh)
        write_credentials(legacy, deadline=deadline, monotonic=monotonic)
        verified = _parse_credentials(_read_secret(CREDENTIALS_ACCOUNT, deadline=deadline, monotonic=monotonic))
        if verified != legacy:
            raise KeychainError("migrated Keychain credential verification failed")
        cleanup_failed = False
        for account in (_LEGACY_ACCESS_ACCOUNT, _LEGACY_REFRESH_ACCOUNT):
            try:
                _delete_secret(account, deadline=deadline, monotonic=monotonic)
            except KeychainError:
                cleanup_failed = True
        if cleanup_failed:
            raise KeychainError("legacy Keychain credential cleanup incomplete")
        return verified


def write_credentials(
    credentials: Credentials,
    *,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    if not credentials.access or not credentials.refresh:
        raise KeychainError("credentials must be non-empty")
    payload = json.dumps(
        {"access": credentials.access, "refresh": credentials.refresh},
        sort_keys=True,
        separators=(",", ":"),
    )
    _write_secret(CREDENTIALS_ACCOUNT, payload, deadline=deadline, monotonic=monotonic)
