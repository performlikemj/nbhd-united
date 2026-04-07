"""Temporary test view for workspace session isolation.

POST /api/v1/test/workspace-sessions/
Header: Authorization: Bearer <nbhd-internal-api-key>

Sends two messages with different `user` params to a tenant's OpenClaw
container, then reports the results. After verifying session isolation,
check the file share sessions.json for new entries.

TODO: DELETE THIS FILE after workspace session isolation is verified.
"""
from __future__ import annotations

import logging
import time

import httpx
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.orchestrator.azure_client import read_key_vault_secret
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

TEST_TENANT_ID = "148ccf1c-ef13-47f8-ada1-a98fa90e14a0"


@csrf_exempt
@require_POST
def test_workspace_session(request):
    """Send test messages with different user params to verify session isolation."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JsonResponse({"error": "missing Bearer token"}, status=401)

    token = auth_header[7:]
    expected = read_key_vault_secret("nbhd-internal-api-key")
    if not expected or token != expected:
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        tenant = Tenant.objects.select_related("user").get(id=TEST_TENANT_ID)
    except Tenant.DoesNotExist:
        return JsonResponse({"error": "tenant not found"}, status=404)

    if not tenant.container_fqdn:
        return JsonResponse({"error": "no container_fqdn"}, status=400)

    url = f"https://{tenant.container_fqdn}/v1/chat/completions"
    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")

    # Phase 1: Seed each session with a unique fact
    # Phase 2: Ask each session to recall — if isolated, they only know their own fact
    phase = request.GET.get("phase", "1")

    if phase == "2":
        test_params = [
            ("8078236299:ws:general", "What secret word did I tell you in my previous message? Reply with ONLY that word, nothing else."),
            ("8078236299:ws:work", "What secret word did I tell you in my previous message? Reply with ONLY that word, nothing else."),
        ]
    else:
        test_params = [
            ("8078236299:ws:general", "Remember this secret word: PINEAPPLE. Reply only: GOT_IT_GENERAL"),
            ("8078236299:ws:work", "Remember this secret word: TELESCOPE. Reply only: GOT_IT_WORK"),
        ]

    results = []
    for user_param, msg in test_params:
        try:
            resp = httpx.post(
                url,
                json={
                    "model": "openclaw",
                    "messages": [{"role": "user", "content": msg}],
                    "user": user_param,
                },
                headers={
                    "Authorization": f"Bearer {expected}",
                    "X-User-Timezone": user_tz,
                    "X-Channel": "test",
                },
                timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
            reply = ""
            if data.get("choices"):
                reply = data["choices"][0].get("message", {}).get("content", "")[:300]
            results.append({
                "user_param": user_param,
                "http_status": resp.status_code,
                "reply": reply,
            })
        except Exception as e:
            results.append({
                "user_param": user_param,
                "http_status": "error",
                "error": str(e)[:500],
            })

        # Respect concurrency=1 — wait between requests
        time.sleep(3)

    return JsonResponse({
        "tenant_id": str(tenant.id),
        "container_fqdn": tenant.container_fqdn,
        "test_results": results,
        "verify": (
            "Run: az storage file download --share-name ws-148ccf1c-ef13-47f8-a "
            "--path agents/main/sessions/sessions.json --account-name stnbhdprod "
            "--dest /tmp/sessions-after.json && cat /tmp/sessions-after.json"
        ),
    })
