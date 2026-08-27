"""Atomic macOS Keychain storage for the production E2E JWT pair."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

SERVICE = "org.nbhd.e2e"
CREDENTIALS_ACCOUNT = "credentials"
_LEGACY_ACCESS_ACCOUNT = "access"
_LEGACY_REFRESH_ACCOUNT = "refresh"


class KeychainError(RuntimeError):
    """A content-free Keychain failure."""


class MissingKeychainItem(KeychainError):
    pass


@dataclass(frozen=True)
class Credentials:
    access: str
    refresh: str


def _read_secret(account: str) -> str:
    """Capture a secret directly into memory; never inherit terminal output."""
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MissingKeychainItem("Keychain credential unavailable")
    secret = result.stdout.rstrip("\r\n")
    if not secret:
        raise KeychainError("Keychain credential empty")
    return secret


def _write_secret(account: str, secret: str) -> None:
    """Write through security's stdin prompt; the secret is never an argv value."""
    if not secret or "\n" in secret or "\r" in secret:
        raise KeychainError("invalid Keychain credential payload")
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", account, "-s", SERVICE, "-w"],
        input=f"{secret}\n",
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise KeychainError("Keychain credential write failed")


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


def read_credentials() -> Credentials:
    """Read the atomic item, migrating the former two-item layout once."""
    try:
        return _parse_credentials(_read_secret(CREDENTIALS_ACCOUNT))
    except MissingKeychainItem as atomic_error:
        try:
            legacy = Credentials(
                access=_read_secret(_LEGACY_ACCESS_ACCOUNT),
                refresh=_read_secret(_LEGACY_REFRESH_ACCOUNT),
            )
        except KeychainError:
            raise atomic_error
        write_credentials(legacy)
        return legacy


def write_credentials(credentials: Credentials) -> None:
    if not credentials.access or not credentials.refresh:
        raise KeychainError("credentials must be non-empty")
    payload = json.dumps(
        {"access": credentials.access, "refresh": credentials.refresh},
        sort_keys=True,
        separators=(",", ":"),
    )
    _write_secret(CREDENTIALS_ACCOUNT, payload)
