# Agent Skills Architecture

> How NBHD United agents use skills to help people with their daily lives.

## Overview

Each NBHD United subscriber gets a personal AI agent powered by OpenClaw. Skills are markdown documents (SKILL.md) that teach the agent *how* to handle specific situations — journaling, weekly reviews, goal tracking, etc.

Skills are **not code**. They're structured instructions that the agent follows conversationally. The agent reads the skill, understands the flow, and guides the user through it. Any data persistence happens via authenticated API calls to the Django backend.

## Architecture

```
nbhd-united/
├── agent-skills/                    # All agent skills live here
│   ├── daily-journal/
│   │   └── SKILL.md
│   ├── weekly-review/
│   │   └── SKILL.md
│   ├── goal-setting/               # (future)
│   │   └── SKILL.md
│   └── ...
├── apps/
│   ├── integrations/               # Runtime API endpoints skills call
│   │   └── runtime_views.py
│   └── ...
└── runtime/
    └── openclaw/
        └── entrypoint.sh           # OpenClaw container entrypoint
```

### How Skills Get to the Agent

During tenant provisioning (`apps/orchestrator/`), the OpenClaw config is generated with the agent's personality and workspace files. Skills from `agent-skills/` are included in the container's workspace so the agent can reference them.

The config generator (`apps/orchestrator/config_generator.py`) should mount `agent-skills/` into the OpenClaw workspace at `/home/node/.openclaw/workspace/skills/`.

## Skill Format

Each skill is a single `SKILL.md` file in its own directory. The format follows OpenClaw conventions:

```markdown
---
name: skill-name
description: One-line description of what this skill does.
---

# Skill Name

## When to Use
- Trigger conditions (what the user says/does that activates this skill)

## When NOT to Use
- Negative examples (prevents false activation)

## Flow
Step-by-step conversation guide for the agent.

## API Integration
Endpoints to call and payload formats.

## Output Format
What structured data gets produced.
```

### Key Principles

1. **Conversational, not technical.** Skills guide a warm conversation — the user never sees skill internals.
2. **Explicit boundaries.** Every skill states when it applies and when it doesn't. Overlap between skills causes confusion.
3. **Structured output.** Skills produce JSON payloads that POST to Django API endpoints. This is how agent conversations become persistent data.
4. **Stateless agent, stateful backend.** The agent doesn't remember between sessions. All state lives in Django models, retrieved via API when needed.

## Skill ↔ Django Integration

Skills interact with the Django backend through the internal runtime API (`apps/integrations/runtime_views.py`). Each OpenClaw container has an auth token scoped to its tenant.

### Data Flow

```
User ↔ Agent (OpenClaw) ↔ Runtime API (Django) ↔ Database
         reads SKILL.md      POST/GET with tenant token
```

### Example: Journaling Skill

1. User says "let's journal"
2. Agent recognizes the trigger, follows `daily-journal/SKILL.md`
3. Agent has a conversation, collects mood/energy/wins/challenges
4. Agent POSTs structured JSON to `POST /api/runtime/journal-entries/`
5. Django creates a `JournalEntry` model instance for that tenant

### API Authentication

Each OpenClaw container gets a `RUNTIME_API_TOKEN` environment variable during provisioning. The agent includes this as `Authorization: Bearer <token>` in API calls. The Django side validates the token and scopes all queries to the tenant.

### Required API Endpoints (to build in Django)

| Endpoint | Method | Purpose | Used by |
|----------|--------|---------|---------|
| `/api/runtime/journal-entries/` | POST | Create journal entry | daily-journal |
| `/api/runtime/journal-entries/` | GET | List entries (filterable by date range) | weekly-review |
| `/api/runtime/weekly-reviews/` | POST | Create weekly review summary | weekly-review |

These endpoints live in `apps/integrations/runtime_views.py` (or a new `apps/journal/` app as the feature set grows).

## Skill Lifecycle

### 1. Creation
- Developer writes `SKILL.md` in `agent-skills/<skill-name>/`
- Follow the format: frontmatter, when/when-not, flow, API, output
- Test conversationally — have the agent use it and see if the flow feels natural

### 2. Testing
- **Manual:** Chat with a test agent that has the skill loaded. Walk through happy path + edge cases.
- **Review criteria:**
  - Does the agent know when to activate this skill?
  - Does it know when NOT to? (crucial — false activation is worse than missing activation)
  - Is the conversation flow warm and natural?
  - Does the API payload match what Django expects?
  - Are error cases handled? (API down, user abandons mid-flow)

### 3. Deployment
- Merge skill PR to `main`
- Next container build picks up the new/updated skill
- Existing containers get updated on next restart or redeployment
- No migration needed — skills are just files, not code

### 4. Updates
- Edit the SKILL.md, PR, merge
- Breaking changes to API payloads require coordinated deploy (Django endpoint + skill update)
- Version skills via git history, not in-file versioning

## Security Considerations

### Container Isolation
- Each tenant's OpenClaw agent runs in its own Azure Container App
- Managed identity is scoped to that tenant only
- `RUNTIME_API_TOKEN` grants access only to that tenant's data

### Skill Safety
- Skills are **authored by us** (NBHD United team), not user-uploaded
- No user-authored skills in v1 — this avoids prompt injection from user-created skill files
- Skills cannot execute arbitrary code — they're conversation guides, not scripts
- Any `!` command blocks or script references in SKILL.md should be flagged in review

### API Scoping
- All runtime API endpoints filter by tenant — a token for Tenant A cannot access Tenant B's data
- Write operations are validated: the agent can't create entries for other tenants
- Rate limiting on runtime API prevents runaway agents from spamming the database

### Data Privacy
- Journal entries and reviews contain personal data
- All data is tenant-scoped and encrypted at rest (Azure managed)
- Agent conversations are ephemeral (OpenClaw doesn't persist chat history by default)
- Users can request data export/deletion through the dashboard

## Future Considerations

- **Skill registry:** As the skill count grows, an index file (`agent-skills/index.md`) could help the agent discover relevant skills
- **User preferences:** Skills could adapt based on user settings (e.g., journal prompt style, review frequency)
- **Scheduled triggers:** OpenClaw cron or Django Celery tasks could prompt the agent to initiate skills (e.g., Sunday evening weekly review)
- **User-contributed skills:** If we ever allow this, full sandboxing + the security scan workflow from OpenClaw's skill-scanner would be required
