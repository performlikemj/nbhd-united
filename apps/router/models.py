"""Router models — maps chat IDs to OpenClaw containers."""
# The router uses Tenant model directly for lookups.
# No additional models needed — Tenant has chat_id (via User)
# and container_fqdn.
