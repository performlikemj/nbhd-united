"""QStash signature verification helper."""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def verify_qstash_signature(request):
    """Verify the request came from QStash using the official SDK."""
    try:
        from qstash import Receiver
    except ImportError:
        logger.error("qstash package is not installed")
        return False

    signature = request.headers.get("Upstash-Signature")
    if not signature:
        logger.warning("QStash request missing Upstash-Signature header")
        return False

    current_key = getattr(settings, "QSTASH_CURRENT_SIGNING_KEY", None)
    next_key = getattr(settings, "QSTASH_NEXT_SIGNING_KEY", None)

    if not current_key:
        logger.error("QSTASH_CURRENT_SIGNING_KEY not configured")
        return False

    try:
        receiver = Receiver(
            current_signing_key=current_key,
            next_signing_key=next_key or current_key,
        )
        body = request.body.decode("utf-8") if request.body else ""
        url = request.build_absolute_uri()
        receiver.verify(signature=signature, body=body, url=url)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("QStash signature verification failed: %s", exc)
        return False
