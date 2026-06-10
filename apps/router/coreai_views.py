"""JWT-gated manifest for the iOS Core AI on-device model bundle.

The big model files are hosted off-Django (Azure Blob / CDN) under
``settings.COREAI_MODEL_BASE_URL``; this endpoint serves only the small manifest
(produced by ``manage.py generate_coreai_manifest``) with per-file sha256 + absolute
download URLs. Returns 404 when no model is configured, so the iOS app falls back to
Apple's on-device model (iOS 26) or the tenant.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class CoreAIModelManifestView(APIView):
    """``GET /api/v1/coreai/model/manifest/`` — the current on-device model manifest.

    Auth is the DRF default (JWT). The response shape the app expects::

        {"name", "version", "total_bytes",
         "files": [{"path", "url", "sha256", "bytes"}, ...]}
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        base_url = (getattr(settings, "COREAI_MODEL_BASE_URL", "") or "").rstrip("/")
        manifest_path = getattr(settings, "COREAI_MODEL_MANIFEST_PATH", "") or ""

        if not base_url or not manifest_path or not Path(manifest_path).is_file():
            # Nothing configured yet — the app gracefully falls back.
            return Response(
                {"detail": "No on-device model is configured."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            manifest = json.loads(Path(manifest_path).read_text())
            name = manifest.get("name", "")
            version = str(manifest.get("version", "1"))
            files = []
            for entry in manifest.get("files", []):
                rel = entry["path"]
                encoded = "/".join(quote(part) for part in rel.split("/"))
                files.append(
                    {
                        "path": rel,
                        "url": f"{base_url}/{quote(name)}/{quote(version)}/{encoded}",
                        "sha256": entry["sha256"],
                        "bytes": entry.get("bytes", 0),
                    }
                )
            # Only sum when the manifest omits a total (dict.get would eval it eagerly).
            total_bytes = manifest.get("total_bytes")
            if total_bytes is None:
                total_bytes = sum(f["bytes"] for f in files)
        except (OSError, ValueError, KeyError, TypeError):
            # A corrupt/partial manifest must NOT be served: the client verifies each
            # file's sha256, so a missing field would yield a broken/unverifiable
            # download. Fail closed with 503 → the app falls back gracefully.
            logger.exception("coreai manifest unreadable/malformed at %s", manifest_path)
            return Response(
                {"detail": "Model manifest unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "name": name,
                "version": version,
                "total_bytes": total_bytes,
                "files": files,
            }
        )
