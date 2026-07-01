"""Shared inbound-image storage for rich-client (iOS) + Telegram channels.

An inbound photo is written to the tenant's Azure File Share workspace at
``workspace/media/inbound/<hash>.<ext>``; the agent is then handed the
container-mounted path via a ``[Photo attached: <path>]`` marker baked into the
message text. Its built-in ``image`` tool reads that local file and a vision
model describes it (see ``CONTINUITY_image_upload.md``). Image bytes NEVER ride
the ``PendingMessage`` payload — only the path reference does — so a large photo
can't bloat the per-tenant queue row.

This module is the single storage chokepoint both the Telegram poller and the
iOS chat ingress route through, so the filename scheme + container path stay
byte-identical across channels.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

# Workspace-relative directory (under the share) and the container mount point
# OpenClaw reads from. The share is mounted into the container at
# ``/home/node/.openclaw``, so the agent must be handed the MOUNTED path, not
# the share-relative one, or its ``image`` tool can't open the file.
INBOUND_MEDIA_DIR = "workspace/media/inbound"
_CONTAINER_WORKSPACE_ROOT = "/home/node/.openclaw"

# Post-decode size cap for an app-uploaded image. A base64 image rides the JSON
# body and inflates ~4/3, so 1.5 MB decoded ≈ 2.0 MB on the wire. NOTE: DRF's
# JSONParser bypasses Django's DATA_UPLOAD_MAX_MEMORY_SIZE, so this cap alone
# does NOT bound the request body — the ingress view enforces a Content-Length
# ceiling (``_MAX_REQUEST_BODY_BYTES``) before materializing the body. iOS
# compresses before upload.
MAX_APP_IMAGE_BYTES = 1_500_000

# Canonical extensions we accept. We sniff DECODED magic bytes rather than
# trusting a client-supplied mime, so a mislabeled or non-image payload can
# never be stored with an image extension (or forwarded to the vision model).
_ALLOWED_EXTS = frozenset({"jpg", "png", "gif", "webp"})


def sniff_image_type(data: bytes) -> str | None:
    """Return the canonical extension for ``data`` by magic bytes, else None.

    Only the web-renderable set a vision model reliably accepts is allowed;
    everything else (HEIC, PDF, SVG, arbitrary bytes) returns None.
    """
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def decode_and_validate_image(
    raw: object, *, max_bytes: int = MAX_APP_IMAGE_BYTES
) -> tuple[bytes | None, str | None, str | None]:
    """Decode + validate a client-supplied image field.

    ``raw`` may be a bare base64 string or an RFC 2397 data URL
    (``data:image/jpeg;base64,...``). Returns ``(data, ext, error)``:

      - ``(None, None, None)``   — no image supplied (absent/blank). NOT an error.
      - ``(None, None, "<code>")`` — a supplied image that failed validation.
      - ``(data, ext, None)``    — valid image bytes + canonical extension.

    Error codes: ``invalid_image`` (not a string / not base64 / empty after
    decode), ``image_too_large`` (decoded > ``max_bytes``),
    ``unsupported_image_type`` (magic bytes not in the allow-list).
    """
    if raw is None:
        return (None, None, None)
    if not isinstance(raw, str):
        return (None, None, "invalid_image")
    s = raw.strip()
    if not s:
        return (None, None, None)

    if s.startswith("data:"):
        # data:<mime>;base64,<payload>
        head, sep, b64 = s.partition(",")
        if not sep or not b64 or "base64" not in head:
            return (None, None, "invalid_image")
    else:
        b64 = s

    # Tolerate RFC 2045 line-wrapped base64 (76-char chunks with CRLF): strip
    # ASCII whitespace so a legitimately encoded image isn't rejected. We keep
    # validate=True afterwards, so genuinely non-alphabet / corrupt data still
    # fails and the magic-byte sniff remains the real gate.
    b64 = "".join(b64.split())

    # Cheap length guard BEFORE decoding, so a pathological string can't force a
    # large allocation just to be rejected. base64 is ~4/3 the decoded size.
    if len(b64) > (max_bytes * 4) // 3 + 1024:
        return (None, None, "image_too_large")

    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return (None, None, "invalid_image")
    if not data:
        return (None, None, "invalid_image")
    if len(data) > max_bytes:
        return (None, None, "image_too_large")

    ext = sniff_image_type(data)
    if ext is None:
        return (None, None, "unsupported_image_type")
    return (data, ext, None)


def store_inbound_image(tenant_id: str, data: bytes, ext: str) -> tuple[str, str]:
    """Write image bytes to the tenant's workspace share; return its paths.

    Returns ``(container_path, workspace_path)``:
      - ``container_path``  — the MOUNTED path to hand the agent in the marker.
      - ``workspace_path``  — the share-relative path (for the attachment ref).

    The filename is content-addressed (sha256 of the first 1 KB) so re-sending
    the same image is idempotent on the share. The binary write bypasses the
    text sanitizer (which would strip NUL/C0 and corrupt a JPEG) and is a single
    atomic PUT via ``upload_workspace_file_binary``.
    """
    # Lazy import: the azure SDK is heavy and this module is imported on the
    # request path. Mirrors the poller's local import.
    from apps.orchestrator.azure_client import upload_workspace_file_binary

    safe_ext = ext if ext in _ALLOWED_EXTS else "jpg"
    name_hash = hashlib.sha256(data[:1024]).hexdigest()[:8]
    filename = f"photo_{name_hash}.{safe_ext}"
    workspace_path = f"{INBOUND_MEDIA_DIR}/{filename}"
    upload_workspace_file_binary(str(tenant_id), workspace_path, data)
    container_path = f"{_CONTAINER_WORKSPACE_ROOT}/{workspace_path}"
    return container_path, workspace_path
