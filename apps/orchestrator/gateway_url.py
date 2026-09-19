"""One URL policy for tenant gateways; local HTTP is strictly opt-in."""

import os
import re

from django.conf import settings

_LOOPBACK = re.compile(r"127\.0\.0\.1:([1-9][0-9]{0,4})\Z")


def gateway_base_url(tenant=None, *, fqdn=None):
    """Keep HTTPS except for synthetic, DEBUG, Azure-mocked IPv4 loopback."""
    host = fqdn if fqdn is not None else tenant.container_fqdn
    match = _LOOPBACK.fullmatch(host)
    local = (
        settings.DEBUG
        and os.environ.get("AZURE_MOCK", "").lower() == "true"
        and getattr(tenant, "is_synthetic", False) is True
        and match is not None
        and int(match[1]) <= 65535
    )
    return f"{'http' if local else 'https'}://{host}"
