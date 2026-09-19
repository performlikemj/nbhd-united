"""Private Unix-socket hand-off from the sautai lane, held only in RAM."""

import json
import os
import socket
import threading

MAX_BYTES = 8192


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
        from django.conf import settings
        from django.db import close_old_connections
        from django.utils import timezone

        from apps.integrations.models import Integration
        from apps.orchestrator.local_test import local_root

        while True:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                try:
                    payload = b""
                    while b"\n" not in payload and len(payload) <= MAX_BYTES:
                        chunk = connection.recv(1024)
                        if not chunk:
                            break
                        payload += chunk
                    if len(payload) > MAX_BYTES:
                        raise ValueError("oversized")
                    handoff = json.loads(payload)
                    if handoff.get("base_url") != "http://127.0.0.1:8000":
                        raise ValueError("host")
                    secret = handoff.get("platform_secret")
                    user_id = handoff.get("sautai_user_id")
                    if not isinstance(secret, str) or not secret or type(user_id) is not int or user_id <= 0:
                        raise ValueError("fields")
                    close_old_connections()
                    tid = os.environ["NBHD_TENANT_ID"]
                    local_root(tid)
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
                    connection.sendall(b'{"accepted":true}\n')
                except Exception:
                    connection.sendall(b'{"accepted":false}\n')
                finally:
                    close_old_connections()

    threading.Thread(target=serve, name="local-sautai-handoff", daemon=True).start()
    return listener
