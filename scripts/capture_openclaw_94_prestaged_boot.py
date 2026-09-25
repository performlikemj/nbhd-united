"""Local isolated real-entrypoint boot proof; never contacts Azure or a tenant.

Run with PYTHONPATH=. <project-python> scripts/capture_openclaw_94_prestaged_boot.py.
Only synthetic query results/token are mocked; signer and image entrypoint are real.
"""

import io
import json
import os
import subprocess
import tarfile
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.update(
    DJANGO_SETTINGS_MODULE="config.settings.development",
    SECRET_KEY="local-contract-test",
    DATABASE_URL="postgres://test_user:test_password@127.0.0.1:55497/oc94_r7",
    AZURE_MOCK="true",
    NBHD_DISABLE_BACKGROUND_THREADS="True",
)
import django

django.setup()
from apps.cron.share_cron_sync import build_signed_crons_doc

IMAGE = "nbhdunited.azurecr.io/nbhd-openclaw:2026.9.4-8ceb89f"
MANIFEST = "sha256:c244ff686863c1e9e4fc9fc55d1fac3b6a746e76039a9632db8ae082a0f21ddc"
NAME = "nbhd-oc94-r7-prestaged-boot"
FIXTURES = Path("apps/orchestrator/fixtures/openclaw_94_cron_contract")


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, capture_output=True, **kwargs).stdout


def main():
    identity = json.loads(docker("image", "inspect", IMAGE))[0]
    assert IMAGE.split(":")[0] + "@" + MANIFEST in identity["RepoDigests"]
    cases = [
        json.loads((FIXTURES / "round_six" / (name + ".json")).read_text()) for name in ("cron-tz", "at-utc-stable")
    ]
    rows = [
        SimpleNamespace(id=i + 700, name=c["declaration"]["name"], data=c["declaration"], enabled=True, managed=True)
        for i, c in enumerate(cases)
    ]
    with (
        patch("apps.cron.models.CronJob.objects.filter") as query,
        patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="local-contract-token"),
    ):
        query.return_value.order_by.return_value = rows
        signed, count = build_signed_crons_doc(SimpleNamespace(id="local-contract"))
    assert count == 2
    config = json.dumps(
        {
            "gateway": {
                "mode": "local",
                "auth": {"mode": "token", "token": "local-contract-token"},
                "bind": "loopback",
            },
            "cron": {"enabled": False},
            "plugins": {"enabled": False},
        }
    ).encode()
    docker(
        "create",
        "--name",
        NAME,
        "--network",
        "none",
        "-e",
        "NODE_OPTIONS=",
        "-e",
        "OPENCLAW_HOME=/tmp/oc94-boot",
        "-e",
        "OPENCLAW_CONFIG_PATH=/tmp/oc94-boot/openclaw.json",
        "-e",
        "OPENCLAW_STATE_DIR=/tmp/oc94-state",
        "-e",
        "NBHD_INTERNAL_API_KEY=local-contract-token",
        IMAGE,
    )
    try:
        # Copy BEFORE starting the unchanged entrypoint; there is no manual add/reconcile.
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            folder = tarfile.TarInfo("oc94-boot")
            folder.type, folder.mode, folder.uid, folder.gid = tarfile.DIRTYPE, 0o700, 1000, 1000
            archive.addfile(folder)
            for name, data in (("openclaw.json", config), ("nbhd-crons.json", signed)):
                info = tarfile.TarInfo("oc94-boot/" + name)
                info.size, info.mode, info.uid, info.gid = len(data), 0o600, 1000, 1000
                archive.addfile(info, io.BytesIO(data))
        docker("cp", "-a", "-", NAME + ":/tmp", input=stream.getvalue())
        docker("start", NAME)
        polls = []
        for _ in range(60):
            time.sleep(5)
            result = subprocess.run(
                ["docker", "exec", "-e", "NODE_OPTIONS=", NAME, "openclaw", "cron", "list", "--all", "--json"],
                capture_output=True,
                timeout=45,
            )
            if result.returncode:
                continue
            listed = json.loads(result.stdout)
            found = [j for j in listed["jobs"] if j.get("declarationKey") in {"nbhd:700", "nbhd:701"}]
            if len(found) == 2:
                polls.append(listed)
                if len(polls) == 2:
                    break
                time.sleep(25)
        assert len(polls) == 2, "boot_jobs_missing"
        ids = [
            {j["declarationKey"]: j["id"] for j in p["jobs"] if j.get("declarationKey") in {"nbhd:700", "nbhd:701"}}
            for p in polls
        ]
        assert ids[0] == ids[1]
        files = {}
        for source, image_path in (
            ("runtime/openclaw/entrypoint.sh", "/usr/local/bin/nbhd-openclaw-entrypoint"),
            ("runtime/openclaw/nbhd-cron-sync.mjs", "/opt/nbhd/nbhd-cron-sync.mjs"),
        ):
            files[source] = sha256(docker("exec", NAME, "cat", image_path)).hexdigest()
            assert files[source] == sha256(Path(source).read_bytes()).hexdigest()
        assert docker("exec", NAME, "cat", "/tmp/oc94-boot/nbhd-crons.json") == signed
        output = {
            "image": IMAGE,
            "manifest": MANIFEST,
            "imageId": identity["Id"],
            "sources": files,
            "isolation": "network none; no ports/mounts; synthetic token; cron firing/plugins disabled",
            "entrypoint": identity["Config"]["Entrypoint"],
            "prestagedBeforeStart": True,
            "manualCronMutations": 0,
            "signedDigest": sha256(signed).hexdigest(),
            "declarations": json.loads(json.loads(signed)["signed"]),
            "polls": polls,
            "stableIds": ids[0],
        }
        (FIXTURES / "prestaged-boot.json").write_text(json.dumps(output, indent=2) + "\n")
        print("PASS: unchanged real entrypoint installed two pre-staged jobs; IDs stable across two polls")
    finally:
        docker("rm", "-f", "-v", NAME)


if __name__ == "__main__":
    main()
