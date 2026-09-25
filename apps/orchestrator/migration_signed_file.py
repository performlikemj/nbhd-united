"""Crash-safe signed cron publication for active migrations only."""

import hashlib
import uuid

from django.conf import settings

from . import azure_client


def publish_signed_file(tenant, data, *, before_publish=lambda: None):
    """Skip identical bytes; upload/read back a unique sibling then rename.

    No fixed temporary name: a stalled/retried upload cannot truncate another
    writer's candidate. Death before rename leaves the last complete file;
    death after rename leaves the complete new file. Never fall back to an
    in-place upload. An interrupted upload may leave its own inert temp file.
    """
    from azure.storage.fileshare import ShareFileClient

    from .storage_credentials import acquire_account_key, run_with_lease

    tenant_id = str(tenant.pk)
    path = "nbhd-crons.json"
    current = azure_client.download_workspace_file_binary(tenant_id, path)
    if current is not None and hashlib.sha256(current).digest() == hashlib.sha256(data).digest():
        return False
    before_publish()
    if azure_client.is_mock():
        # Keep local mocked storage behind the existing transport seam.
        azure_client._put_share_file(tenant_id, path, data=data, ensure_dirs=False)
        return True
    lease = acquire_account_key(tenant_id)

    def publish(account_key):
        temporary = path + ".migration-" + uuid.uuid4().hex + ".tmp"
        with ShareFileClient(
            account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.file.core.windows.net",
            share_name=f"ws-{tenant_id[:20]}",
            file_path=temporary,
            credential=account_key,
        ) as client:
            client.upload_file(data, length=len(data))
            if client.download_file().readall() != data:
                raise RuntimeError("signed_temporary_readback_mismatch")
            before_publish()
            client.rename_file(path, overwrite=True)

    run_with_lease(tenant_id, lease, publish)
    return True
