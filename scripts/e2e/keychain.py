"""Small macOS Keychain wrapper that never places secrets in argv or output."""

from __future__ import annotations

import subprocess

SERVICE = "org.nbhd.e2e"
ACCESS_ACCOUNT = "access"
REFRESH_ACCOUNT = "refresh"
_ACCOUNTS = {ACCESS_ACCOUNT, REFRESH_ACCOUNT}


class KeychainError(RuntimeError):
    """A content-free Keychain failure."""


def _validate_account(account: str) -> None:
    if account not in _ACCOUNTS:
        raise ValueError("unsupported Keychain account")


def read_secret(account: str) -> str:
    """Capture a secret directly into memory; never inherit terminal output."""
    _validate_account(account)
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise KeychainError(f"Keychain item unavailable for account={account}")
    secret = result.stdout.rstrip("\r\n")
    if not secret:
        raise KeychainError(f"Keychain item empty for account={account}")
    return secret


def write_secret(account: str, secret: str) -> None:
    """Write through security's stdin prompt; the secret is never an argv value."""
    _validate_account(account)
    if not secret or "\n" in secret or "\r" in secret:
        raise ValueError("invalid secret")
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", account, "-s", SERVICE, "-w"],
        input=f"{secret}\n",
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise KeychainError(f"Keychain write failed for account={account}")


def read_tokens() -> tuple[str, str]:
    return read_secret(ACCESS_ACCOUNT), read_secret(REFRESH_ACCOUNT)


def write_tokens(access: str, refresh: str) -> None:
    write_secret(ACCESS_ACCOUNT, access)
    write_secret(REFRESH_ACCOUNT, refresh)
