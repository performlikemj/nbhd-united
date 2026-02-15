# Composio Integration

## Overview

Composio manages OAuth authentication for **Gmail** and **Google Calendar** integrations.
Instead of storing Google OAuth tokens directly in Azure Key Vault, these two providers
delegate token lifecycle (issue, refresh, revoke) to the Composio platform.

Non-Composio providers (e.g. Sautai) continue to use the legacy Key Vault path.

### Why Composio?

Google OAuth tokens require periodic refresh and careful scope management. Composio
handles this automatically, so we only need to retrieve a valid access token on demand.

## SDK Packages

| Package            | Version | Role                                       |
|--------------------|---------|---------------------------------------------|
| `composio`         | 0.11.1  | High-level SDK: `Composio` class, models    |
| `composio-client`  | 1.27.0  | Low-level HTTP client (Stainless-generated) |

The high-level SDK wraps the low-level client. For example:

```
composio.sdk.Composio                     # high-level class
  .connected_accounts                      # composio.core.models.ConnectedAccounts
    .get = client.connected_accounts.retrieve   # alias to low-level
    .delete = client.connected_accounts.delete
    .list = client.connected_accounts.list
```

## Django Settings

| Setting                             | Purpose                                         |
|-------------------------------------|-------------------------------------------------|
| `COMPOSIO_API_KEY`                  | API key for the Composio SDK                    |
| `COMPOSIO_GMAIL_AUTH_CONFIG_ID`     | Auth config ID for Gmail in Composio dashboard  |
| `COMPOSIO_GCAL_AUTH_CONFIG_ID`      | Auth config ID for Google Calendar              |
| `COMPOSIO_ALLOW_MULTIPLE_ACCOUNTS`  | Allow multiple connected accounts per tenant    |

## Database Model

`apps/integrations/models.py` — `Integration`

Composio-managed integrations store a `composio_connected_account_id` (a Composio nano-ID)
and leave `key_vault_secret_name` blank. The `status` field tracks whether the integration
is ACTIVE, ERROR, EXPIRED, or REVOKED.

## Code Architecture

All Composio logic lives in `apps/integrations/services.py`.

### Connection Flow

```
initiate_composio_connection(tenant, provider, callback_url)
    -> client.connected_accounts.initiate(user_id, auth_config_id, ...)
    -> returns (redirect_url, connection_request_id)

complete_composio_connection(tenant, provider, connection_request_id)
    -> client.connected_accounts.wait_for_connection(id, timeout)
    -> _extract_composio_email(connected_account.id)
    -> Integration.objects.update_or_create(...)
```

1. The frontend calls `initiate_composio_connection()`, which returns a redirect URL.
2. The user authenticates with Google via Composio's OAuth flow.
3. The callback triggers `complete_composio_connection()`, which polls until the
   connection becomes ACTIVE, extracts the user's email, and persists the integration.

### Token Retrieval Flow

```
get_valid_provider_access_token(tenant, provider)
    -> is_composio_provider(provider)?
       YES -> _get_composio_access_token(integration, tenant, provider)
       NO  -> Key Vault path (load, refresh, etc.)
```

`_get_composio_access_token()` calls the Composio API to retrieve a fresh access token
on every invocation. Composio handles OAuth refresh internally, so we never store or
manage refresh tokens for these providers.

### Disconnection Flow

```
disconnect_integration(tenant, provider)
    -> client.connected_accounts.delete(connected_account_id)
    -> Integration.objects.update(status=REVOKED)
```

### Error Recovery

The Composio path intentionally skips the strict `status=ACTIVE` check that the
Key Vault path uses. If a previous call failed and marked the integration as ERROR,
the next successful token retrieval auto-recovers the status back to ACTIVE. This
prevents transient Composio API failures from permanently blocking an integration.

### Credential Masking

Composio projects default to `mask_secret_keys_in_connected_account = True`, which
causes `connected_accounts.get()` to return truncated tokens like `"ya29.a0AfH..."`.

Our `_get_composio_client()` singleton automatically disables this on first init via:

```python
client._client.project.config.update(
    mask_secret_keys_in_connected_account=False,
)
```

This calls `PATCH /api/v3/org/project/config` once per process lifetime. The setting
is project-wide and idempotent. If the call fails (e.g. permissions), a warning is
logged and token retrieval will hit the masked-token check, raising
`IntegrationTokenDataError`.

To check or change this setting manually:

```python
from composio import Composio
c = Composio(api_key="...")

# Read current config
config = c._client.project.config.retrieve()
print(config.mask_secret_keys_in_connected_account)

# Disable masking
c._client.project.config.update(mask_secret_keys_in_connected_account=False)
```

Or toggle it in the Composio dashboard: **Settings > Mask Connected Account Secrets**.

## SDK API Reference

### Critical: Positional `nanoid` Parameter

The low-level `composio_client` methods use `nanoid` as a **positional** parameter.
The high-level SDK aliases (`.get`, `.delete`) pass arguments straight through.

```python
# Correct — positional arg
client.connected_accounts.get(connected_account_id)
client.connected_accounts.delete(connected_account_id)

# WRONG — these will raise TypeError
client.connected_accounts.get(id=connected_account_id)
client.connected_accounts.delete(account_id=connected_account_id)
```

Low-level method signatures (`composio_client/resources/connected_accounts.py`):

| Method          | Signature                        | Notes                          |
|-----------------|----------------------------------|--------------------------------|
| `retrieve`      | `(self, nanoid: str, *, ...)`    | Aliased as `.get` in SDK       |
| `delete`        | `(self, nanoid: str, *, ...)`    | Positional only                |
| `update_status` | `(self, nano_id: str, *, ...)`   | Note: `nano_id` not `nanoid`   |
| `refresh`       | `(self, nanoid: str, *, ...)`    | Triggers re-auth flow          |

The `wait_for_connection` method on the high-level `ConnectedAccounts` class does use
`id` as its parameter name, so `wait_for_connection(id=..., timeout=...)` is correct.

### Response Model: `ConnectedAccountRetrieveResponse`

```
account.id          -> str (nano-ID)
account.status      -> "ACTIVE" | "INITIALIZING" | "INITIATED" | "FAILED" | "EXPIRED" | "INACTIVE"
account.state       -> State (union type, discriminated by auth_scheme)
account.state.val   -> Pydantic BaseModel (NOT a dict)
account.data        -> deprecated, use state instead
```

### `state.val` — Pydantic Model, Not a Dict

`state.val` is a **Pydantic BaseModel** (e.g. `StateUnionMember1ValUnionMember2` for
an ACTIVE OAuth2 connection). It has typed fields and allows extra properties via
`__pydantic_extra__`.

Known fields on the ACTIVE OAuth2 variant:

| Field           | Type                        | Notes                        |
|-----------------|-----------------------------|------------------------------|
| `access_token`  | `str`                       | Required, the OAuth token    |
| `status`        | `Literal["ACTIVE"]`         | Always "ACTIVE" in this case |
| `refresh_token` | `Optional[str]`             |                              |
| `token_type`    | `Optional[str]`             |                              |
| `expires_in`    | `Union[float, str, None]`   |                              |
| `scope`         | `Union[str, List[str], None]`|                              |
| `id_token`      | `Optional[str]`             |                              |

Extra properties (like `email`) land in `__pydantic_extra__` and are accessible via
`getattr()` or `model_dump()`.

Our `_composio_val_to_dict()` helper normalises `state.val` to a plain dict:

```python
def _composio_val_to_dict(account) -> dict:
    val = getattr(getattr(account, "state", None), "val", None)
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if hasattr(val, "model_dump"):
        return val.model_dump()
    return {}
```

This handles Pydantic models, plain dicts (defensive), and None gracefully.

### Masked Tokens

By default, Composio masks sensitive fields in API responses (e.g. `"ya29.a0AfH6S..."`).
Our code detects this with `access_token.endswith("...")` and raises
`IntegrationTokenDataError`. If this happens in production, check the Composio dashboard
setting: **Settings > Mask Connected Account Secrets** must be **disabled**.

## Testing

Tests live in `apps/integrations/test_services.py`, class `ComposioConnectedAccountsAPITest`.

All Composio API calls are mocked via `@patch("apps.integrations.services._get_composio_client")`.
Mock `state.val` as a plain dict — the `_composio_val_to_dict()` helper's `isinstance(val, dict)`
branch handles it without needing to construct real Pydantic models.

```python
state = Mock()
state.val = {"access_token": "real-token", "email": "user@gmail.com"}
account = Mock(state=state)
mock_get_client.return_value.connected_accounts.get.return_value = account
```

Run tests:

```bash
DJANGO_SETTINGS_MODULE=config.settings.development \
  .venv/bin/python -m django test apps.integrations --keepdb
```

## Troubleshooting

| Symptom                                         | Cause                                    | Fix                                              |
|-------------------------------------------------|------------------------------------------|--------------------------------------------------|
| `TypeError: got unexpected keyword argument`    | Using `id=` or `account_id=` kwargs      | Pass the nano-ID as a positional argument         |
| `AttributeError: ... has no attribute 'get'`    | Treating `state.val` as a dict           | Use `_composio_val_to_dict()` helper              |
| `AttributeError: ... no attribute 'get_auth_params'` | Old SDK API (pre-v0.11)            | Use `connected_accounts.get()` + `state.val`      |
| `IntegrationTokenDataError: masked/empty token` | Composio secret masking enabled          | Disable in Composio dashboard settings            |
| Token works once then fails                     | Composio account went EXPIRED/INACTIVE   | Check account status in Composio dashboard        |
