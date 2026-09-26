"""Private Unix-socket hand-off from the sautai lane, held only in RAM."""

import json
import os
import socket
import threading

MAX_BYTES = 8192


def accept_payload(handoff):
    """Validate before changing either RAM settings or the optional fixture link."""
    from django.conf import settings
    from django.utils import timezone

    from apps.integrations.models import Integration
    from apps.orchestrator.local_test import local_root

    if not isinstance(handoff, dict) or handoff.get("base_url") != "http://127.0.0.1:8000":
        raise ValueError("host")
    secret = handoff.get("platform_secret")
    user_id = handoff.get("sautai_user_id")
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret")
    if user_id is not None and (type(user_id) is not int or user_id <= 0):
        raise ValueError("identity")
    tid = os.environ["NBHD_TENANT_ID"]
    if local_root(tid) is None:
        raise ValueError("local_stack_required")
    if user_id is not None:
        Integration.objects.update_or_create(
            tenant_id=tid,
            provider=Integration.Provider.SAUTAI,
            defaults={
                "sautai_user_id": user_id,
                "linked_at": timezone.now(),
                "status": Integration.Status.ACTIVE,
            },
        )
    settings.SAUTAI_PLATFORM_SECRET = secret


def start_listener(state):
    path = state / "sautai-handoff.sock"
    if path.exists():
        # Never remove an active lane's listener.
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
        else:
            raise RuntimeError("Another hand-off listener is active")
        finally:
            probe.close()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(2)

    def serve():
        from django.db import close_old_connections

        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                accepted = False
                try:
                    payload = b""
                    while b"\n" not in payload and len(payload) <= MAX_BYTES:
                        chunk = connection.recv(1024)
                        if not chunk:
                            break
                        payload += chunk
                    if len(payload) > MAX_BYTES:
                        raise ValueError("oversized")
                    close_old_connections()
                    accept_payload(json.loads(payload))
                    accepted = True
                except Exception:
                    pass  # Never expose a token or database exception in diagnostics.
                finally:
                    close_old_connections()
                try:
                    connection.sendall(b'{"accepted":true}\n' if accepted else b'{"accepted":false}\n')
                except OSError:
                    pass  # A disconnected probe/client must not kill the listener.

    threading.Thread(target=serve, name="local-sautai-handoff", daemon=True).start()
    return listener
