#!/usr/bin/env python3
"""Fixed local synthetic chat proof. No response, password or token output."""

import argparse
import getpass
import json
import runpy
import time
import uuid
from pathlib import Path

import httpx

RUNNER = runpy.run_path(str(Path(__file__).with_name("run.py")))
BASE = "http://127.0.0.1:18080"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    expected = RUNNER["environment"]()["NBHD_TENANT_ID"]
    with httpx.Client(base_url=BASE, follow_redirects=False, trust_env=False, timeout=180) as client:
        password = getpass.getpass("MJ account password (memory only): ")
        response = client.post("/api/v1/auth/login/", json={"email": args.email, "password": password})
        del password
        if response.status_code != 200:
            raise RuntimeError("Local login failed")
        client.headers["Authorization"] = "Bearer " + response.json()["access"]
        del response
        response = client.get("/api/v1/tenants/me/")
        if response.status_code != 200:
            raise RuntimeError("Tenant gate unavailable")
        tenant = response.json()
        if (
            tenant.get("id") != expected
            or tenant.get("is_synthetic") is not True
            or tenant.get("is_eval_sink") is not False
        ):
            raise RuntimeError("Synthetic tenant gate failed")
        response = client.post("/api/v1/chat/threads/", json={"title": "[NBHD E2E SYNTHETIC] Local proof"})
        if response.status_code != 201:
            raise RuntimeError("Disposable thread creation failed")
        thread = response.json()["id"]
        if response.json().get("is_main") is not False:
            raise RuntimeError("Refuse main thread")
        try:
            message = str(uuid.uuid4())
            response = client.post(
                "/api/v1/chat/messages/",
                json={
                    "client_msg_id": message,
                    "thread_id": thread,
                    "text": "[NBHD E2E SYNTHETIC] Reply with one short greeting. Do not use tools or contact anyone.",
                },
            )
            if response.status_code not in (200, 201, 202):
                raise RuntimeError("Chat admission failed")
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                response = client.get(f"/api/v1/chat/messages/{message}/")
                if response.status_code != 200:
                    raise RuntimeError("Chat poll failed")
                data = response.json()
                if data.get("status") == "ready":
                    ok = (
                        data.get("source") == "tenant"
                        and not data.get("error")
                        and bool(str(data.get("reply_text", "")).strip())
                    )
                    if not ok:
                        raise RuntimeError("Reply failed tenant/content/error gate")
                    print(
                        json.dumps(
                            {
                                "proof": "chat",
                                "status": "ready",
                                "source": "tenant",
                                "reply_nonempty": True,
                                "error_empty": True,
                            }
                        )
                    )
                    return
                if data.get("status") in ("failed", "error"):
                    raise RuntimeError("Chat terminal failure")
                time.sleep(1.5)
            raise RuntimeError("Chat proof deadline exceeded")
        finally:
            response = client.delete(f"/api/v1/chat/threads/{thread}/")
            print(json.dumps({"cleanup": "thread", "deleted": response.status_code in (200, 204)}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"proof": "chat", "status": "failed"}))
        raise SystemExit(1) from None
