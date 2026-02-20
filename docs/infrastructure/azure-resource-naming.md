# Azure Resource Naming Conventions

Per-tenant Azure resources are created programmatically during provisioning
(`apps/orchestrator/services.py` and `apps/orchestrator/azure_client.py`).
Each resource type uses a **distinct prefix** so you can identify what a
resource is at a glance in the Azure portal or CLI output.

## Naming Patterns

All names are derived from the tenant UUID, truncated to 20 characters.

| Resource | Prefix | Example |
|---|---|---|
| Container App | `oc-` | `oc-148ccf1c-ef13-47f8-a` |
| Managed Identity | `mi-nbhd-` | `mi-nbhd-148ccf1c-ef13-47f8-a` |
| File Share / Storage Mount | `ws-` | `ws-148ccf1c-ef13-47f8-a` |

## Why the Prefixes Differ

Container Apps and Managed Identities are separate Azure resource types that
live in the same resource group. Using different prefixes avoids ambiguity
and makes it obvious which resource you're referencing:

- **`oc-`** (OpenClaw) — the running container for a tenant.
- **`mi-nbhd-`** (Managed Identity) — the identity assigned to that
  container, used for Key Vault secret access and ACR image pulls.
- **`ws-`** (Workspace) — the Azure File Share mounted into the container
  as persistent storage.

Because the prefixes differ, you cannot swap one for another when
constructing Azure resource IDs. For example, when referencing a Key Vault
secret via `identityref:`, you must use the `mi-nbhd-` identity name — not
the `oc-` container name.

## RBAC Roles Assigned to Each Identity

During provisioning, each managed identity is granted two scoped roles:

1. **Key Vault Secrets User** — scoped to the project Key Vault, so the
   container can resolve `keyvaultref:` secrets at runtime.
2. **AcrPull** — scoped to the project Container Registry, so the container
   can pull its image.

## Quick Reference: CLI Lookups

List all managed identities in the resource group:

```bash
az identity list --resource-group <RG> --query "[].name" -o tsv
```

Find the identity for a specific container app:

```bash
# Given container app name "oc-<TENANT_ID_PREFIX>",
# the identity is "mi-nbhd-<TENANT_ID_PREFIX>"
az identity show --resource-group <RG> --name mi-nbhd-<TENANT_ID_PREFIX>
```
