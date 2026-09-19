#!/usr/bin/env python3
"""Basecamp prerequisite install, contained in this worktree."""

import hashlib
import os
import platform
import shutil
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deploy/local-test/.state"


def main():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This installer targets basecamp (Apple Silicon macOS)")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {**os.environ, "UV_CACHE_DIR": str(STATE / "uv-cache"), "PYTHONDONTWRITEBYTECODE": "1"}
    if not (ROOT / ".venv/bin/python").exists():
        subprocess.run(
            ["uv", "venv", str(ROOT / ".venv"), "--python", "/Users/mjjones/.local/bin/python3.12"], check=True, env=env
        )
    # Reuse installed dependencies read-only. No pip mutation of the primary tree.
    packages = Path("/Users/mjjones/Projects/nbhd-united/.venv/lib/python3.12/site-packages")
    if not packages.is_dir():
        raise SystemExit("Basecamp repo dependency venv missing; install dependencies into this worktree first")
    (ROOT / ".venv/lib/python3.12/site-packages/basecamp-dependencies.pth").write_text(str(packages) + "\n")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(ROOT / ".venv/bin/python"), "ruff==0.15.21"], check=True, env=env
    )
    plugin = STATE / "docker/cli-plugins/docker-compose"
    if not plugin.exists():
        url = "https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-darwin-aarch64"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        with urllib.request.urlopen(url + ".sha256", timeout=20) as response:
            digest = response.read().decode().split()[0]
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError("Compose checksum mismatch")
        plugin.parent.mkdir(parents=True, exist_ok=True)
        plugin.write_bytes(data)
        plugin.chmod(0o700)
    subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "deploy/local-test/install.py")], check=True, env=env)
    model_root = Path("/Users/mjjones/.cache/huggingface/hub/models--lakshyakh93--deberta_finetuned_pii")
    revision = (model_root / "refs/main").read_text().strip()
    source = model_root / "snapshots" / revision
    destination = Path("/Users/mjjones/openclaw-yuki-test/pii-model")
    if not destination.exists():
        shutil.copytree(source, destination)


if __name__ == "__main__":
    main()
