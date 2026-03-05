# Google Workspace Integration via `gws` CLI

**Status:** In progress
**Branch:** `feat/gws-integration`
**Date:** 2026-03-05

## Overview

Replace custom Composio Google integrations with Google's official `gws` CLI.
Gives subscribers Gmail, Calendar, Drive, Docs, Sheets access through their
AI agent — all via one tool with structured JSON output.

## Architecture

```
User (frontend) → "Connect Google" → OAuth consent screen → callback to Django
Django → encrypts & stores credentials on tenant's file share
Container → reads creds via GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE env var
Agent → uses gws skills (gmail +triage, calendar +agenda, drive files list, etc.)
```

## Implementation

### 1. Container Image — Install gws

Add to Dockerfile:
```dockerfile
RUN npm install -g @googleworkspace/cli
```

### 2. GWS Skills — Copy to Container

Copy the subset of skills we want subscribers to have access to:
- `gws-shared` (required base — auth, flags, security rules)
- `gws-gmail` + `gws-gmail-send` + `gws-gmail-triage`
- `gws-calendar` + `gws-calendar-agenda` + `gws-calendar-insert`
- `gws-drive` + `gws-drive-upload`
- `gws-tasks`

Skills go into the container's OpenClaw skills directory.

### 3. OAuth Flow (Django backend)

#### New Django model: `GoogleWorkspaceCredential`
```python
class GoogleWorkspaceCredential(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    credentials_encrypted = models.BinaryField()  # AES-256 encrypted OAuth creds
    scopes = models.JSONField(default=list)
    connected_at = models.DateTimeField(auto_now_add=True)
    email = models.EmailField(blank=True)  # Google account email
```

#### OAuth endpoints
- `POST /api/v1/tenants/google/connect/` — Initiates OAuth flow, returns auth URL
- `GET /api/v1/tenants/google/callback/` — OAuth callback, stores encrypted creds
- `GET /api/v1/tenants/google/status/` — Check connection status
- `POST /api/v1/tenants/google/disconnect/` — Remove credentials

#### Flow:
1. Frontend: user clicks "Connect Google"
2. Django generates OAuth URL with Gmail+Calendar+Drive scopes
3. User consents in Google popup
4. Google redirects to our callback URL
5. Django receives auth code → exchanges for tokens
6. Django encrypts tokens → stores in DB
7. Django writes `gws-credentials.json` to tenant's Azure file share
8. Container's OpenClaw picks up creds via env var on next restart
   (or we signal the container to reload)

### 4. Credential Delivery to Container

Two paths, use both:

**A. File share (persistent):**
Write `gws-credentials.json` to tenant's file share at
`ws-<tenant_uuid>/gws-credentials.json`

**B. Env var (pointer):**
Set `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/workspace/gws-credentials.json`
in the container config.

The file share is already mounted at `/workspace` (or wherever OpenClaw's
workspace dir is). No new Azure infrastructure needed.

### 5. Config Generator Updates

When `GoogleWorkspaceCredential` exists for a tenant:
- Add gws skills to the OpenClaw config
- Set `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` env var
- Add tool policy allowing gws commands

When not connected:
- Don't include gws skills (agent won't know about them)

### 6. Frontend

Settings → Integrations page (same page as LINE):
- "Connect Google" button → OAuth popup
- Shows connected Google account email
- Disconnect button
- Scopes summary (Gmail, Calendar, Drive)

### 7. GCP Project Setup

Need one GCP project for NBHD United:
- OAuth consent screen (External, production)
- OAuth client ID (Web application type)
- Redirect URI: `https://neighborhoodunited.org/api/v1/tenants/google/callback/`
- Scopes: gmail.modify, calendar, drive.file

Store in Key Vault:
- `google-oauth-client-id`
- `google-oauth-client-secret`

## Security

- Credentials encrypted at rest in DB (same pattern as other secrets)
- File share credentials in plain JSON (same security as OpenClaw config — 
  file share is per-tenant, isolated)
- OAuth refresh tokens auto-renew
- Disconnect removes creds from DB AND file share
- Agent tool policy prevents credential exfiltration

## Testing Strategy

1. OAuth flow unit tests (mock Google endpoints)
2. Credential storage/retrieval tests
3. File share write tests (mock Azure Storage)
4. Config generator tests (with/without Google connected)
5. Frontend component tests

## Rollout

Phase 1 (this PR): Gmail + Calendar
Phase 2: Drive, Docs, Sheets
Phase 3: Tasks, Keep
