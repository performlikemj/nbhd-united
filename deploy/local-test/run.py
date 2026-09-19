#!/usr/bin/env python3
"""Clean-room launcher; credentials never enter argv or launchd plists."""

import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deploy/local-test/.state"
TEST_HOME = Path("/Users/mjjones/openclaw-yuki-test")
PYTHON = str(ROOT / ".venv/bin/python")


def environment():
    values = {}
    for line in (ROOT / ".env.local-test").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if values.get("AZURE_MOCK") != "true" or values.get("DJANGO_SETTINGS_MODULE") != "config.settings.local_test":
        raise RuntimeError("Refusing unsafe environment")
    db = urlsplit(values.get("DATABASE_URL", ""))
    if (db.hostname, db.port, db.path, db.username) != ("127.0.0.1", 55441, "/nbhd_yuki_test", "nbhd_yuki_test"):
        raise RuntimeError("Refusing any database except the dedicated Compose database")
    if values.get("ADMIN_DATABASE_URL") != values["DATABASE_URL"]:
        raise RuntimeError("Admin database must be the same dedicated Compose database")
    if TEST_HOME.is_symlink() or values.get("LOCAL_TEST_ROOT") != str(TEST_HOME):
        raise RuntimeError("Refusing unexpected test home")
    for key, expected in {
        "OPENCLAW_HOME": str(TEST_HOME),
        "OPENCLAW_STATE_DIR": str(TEST_HOME / "state"),
        "OPENCLAW_CONFIG_PATH": str(TEST_HOME / "openclaw.json"),
        "NBHD_API_BASE_URL": "http://127.0.0.1:18080",
        "API_BASE_URL": "http://127.0.0.1:18080",
        "ADMIN_OPENCLAW_GATEWAY_URL": "",
        "ADMIN_OPENCLAW_GATEWAY_TOKEN": "",
    }.items():
        if values.get(key) != expected:
            raise RuntimeError("Refusing unexpected local integration configuration")
    # No ambient provider tokens, proxy variables, personal OpenClaw state, or .env.
    if (ROOT / ".env").exists():
        raise RuntimeError("Remove .env from this worktree; local-test uses only .env.local-test")
    return {
        "PATH": "/Users/mjjones/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(TEST_HOME),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT / "deploy/local-test") + os.pathsep + str(ROOT),
        "NBHD_TEST_DB_REUSE": "1" if os.environ.get("NBHD_TEST_DB_REUSE") == "1" else "0",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TMPDIR": str(STATE / "tmp"),
        "NODE_OPTIONS": "--import=" + str(ROOT / "deploy/local-test/openclaw-paths.mjs"),
        **values,
    }


def network_guard(event, args):
    if event == "socket.getaddrinfo" and args[0] not in ("127.0.0.1", "localhost", "::1", None):
        raise PermissionError("Local test stack refused external DNS")
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple):
            if address[0] not in ("127.0.0.1", "::1") or address[1] not in (18080, 19443, 11434, 8000, 55441, 56381):
                raise PermissionError("Local test stack refused outbound network connection")
        elif not str(address).startswith(str(STATE)):
            raise PermissionError("Local test stack refused external Unix socket")


def main():
    os.umask(0o077)
    os.chdir(ROOT)
    command, *args = sys.argv[1:]
    if command == "password-help":
        print("MJ only: security add-generic-password -s org.nbhd.yuki-test -a nbhd -w")
        print("Enter the account password at the interactive prompt. Never put it in argv or .env.")
        return
    env = environment()
    if not os.environ.get("LOCAL_TEST_CLEAN_PROCESS"):
        env["LOCAL_TEST_CLEAN_PROCESS"] = "1"
        os.execve(PYTHON, [PYTHON, str(Path(__file__).resolve()), command, *args], env)
    sys.path.insert(0, str(ROOT))
    if command == "manage":
        if args and args[0] == "test":
            os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.local_test_checks"
            digest = hashlib.sha256(os.fsencode(ROOT)).hexdigest()[:6]
            os.environ["DJANGO_TEST_DB_NAME"] = "test_nbhd_united_yuki_test_" + digest
            os.execve("/bin/bash", ["bash", str(ROOT / "scripts/test-local.sh"), *args[1:]], dict(os.environ))
        sys.argv = ["manage.py", *args]
        runpy.run_path(str(ROOT / "manage.py"), run_name="__main__")
    elif command == "django":
        import django

        django.setup()
        handoff = runpy.run_path(str(ROOT / "deploy/local-test/handoff.py"))
        handoff["start_listener"](STATE)
        sys.argv = ["manage.py", "runserver", "127.0.0.1:18080", "--noreload"]
        runpy.run_path(str(ROOT / "manage.py"), run_name="__main__")
    elif command == "gateway":
        processes = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, text=True, check=True).stdout
        if any("loanarmy" in line.lower() for line in processes.splitlines()):
            raise RuntimeError("Loanarmy process present; refuse to start GPU inference gateway")
        if not (TEST_HOME / "openclaw.json").exists():
            raise RuntimeError("MJ signup and provision step must complete first")
        # Seatbelt denies remote network from Node and all plugin subprocesses.
        profile = ROOT / "deploy/local-test/gateway.sb"
        os.execve(
            "/usr/bin/sandbox-exec",
            ["sandbox-exec", "-f", str(profile), "/Users/mjjones/.local/bin/openclaw", "gateway", "run"],
            dict(os.environ),
        )
    elif command == "sautai-handoff":
        import socket

        payload = sys.stdin.buffer.readline(8193)
        if len(payload) > 8192:
            raise RuntimeError("Hand-off exceeds limit")
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(10)
            connection.connect(str(STATE / "sautai-handoff.sock"))
            connection.sendall(payload)
            result = json.loads(connection.recv(1024))
            print(json.dumps({"handoff_accepted": result.get("accepted") is True}))
            if result.get("accepted") is not True:
                raise SystemExit(1)

    else:
        raise SystemExit("Commands: manage, django, gateway, sautai-handoff, password-help")


if __name__ == "__main__":
    main()
