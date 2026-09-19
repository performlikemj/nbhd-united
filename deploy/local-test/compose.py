#!/usr/bin/env python3
"""Use the repo Compose with isolated project, volume, network and host ports."""

import runpy
import subprocess
import sys
from pathlib import Path

runner = runpy.run_path(str(Path(__file__).with_name("run.py")))
root = runner["ROOT"]
env = runner["environment"]()
# Docker socket is infrastructure, never inherited into Django or gateway.
env["DOCKER_HOST"] = "unix:///Users/mjjones/.colima/default/docker.sock"
args = sys.argv[1:] or ["up", "-d", "postgres", "redis"]
if args not in (["up", "-d", "postgres", "redis"], ["ps"], ["stop", "postgres", "redis"]):
    raise SystemExit("Allowed: up -d postgres redis | ps | stop postgres redis")
subprocess.run(
    [
        "/Users/mjjones/.local/bin/docker",
        "--config",
        str(root / "deploy/local-test/.state/docker"),
        "compose",
        "--project-name",
        "nbhd-yuki-test",
        "--env-file",
        str(root / ".env.local-test"),
        "-f",
        str(root / "docker-compose.yml"),
        "-f",
        str(Path(__file__).with_name("compose.yaml")),
        *args,
    ],
    cwd=root,
    env=env,
    check=True,
)
