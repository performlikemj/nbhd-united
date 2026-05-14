/**
 * NBHD Routing Context Plugin
 *
 * Two responsibilities, both backed by OpenClaw plugin hooks (2026.5.7+):
 *
 * 1. `before_prompt_build` — inject the tenant's workspace catalogue into
 *    the agent's system prompt every turn, with explicit guidance to call
 *    `nbhd_workspace_switch` if the user's message doesn't fit the active
 *    workspace. Replaces the bare `[Active workspace: X]\n` marker that
 *    Django built in `apps/router/workspace_routing.py:build_workspace_context_marker`
 *    — that marker hid every other workspace from the LLM, which is the
 *    proximate cause of the 2026-05-14 routing trap. See
 *    CONTINUITY_workspace-routing-fix.md, Phase 3.
 *
 * 2. `before_agent_finalize` + `message_sending` — reject corrupted model
 *    output (token-loop degeneration, system-prompt echo, doubled-date
 *    artifacts) before it reaches the user. See Phase 4.
 *
 * Hook contract verified against `dist/plugin-sdk/src/plugins/hook-types.d.ts`
 * in openclaw@2026.5.7.
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 2000;
const DEFAULT_CACHE_TTL_MS = 15_000;

// Heuristics that catch the 2026-05-14 corruption pattern (token loop +
// system-prompt echo). Tuned against the actual 3:05 PM JST reply:
//   "[Now: 2026-2026-05-14 15:06 JST (Thursday)]
//    [chat: user is mid-conversation, reply concisely...]
//    [Active workspace: _sync:Heartbeat Check-in]
//    User User User midUser working User UserUserUser..."
const SYSTEM_PROMPT_ECHO_PATTERNS = [
  /\[Now:\s*\d/,
  /\[Active workspace:/i,
  /\[chat:\s*user is mid-conversation/i,
];
// Detect "User User User User User User User User User" — same word >=8
// times in a row, separated only by whitespace. Lower bound chosen so
// occasional double-words ("the the") and intentional emphasis don't trip.
const REPEATED_WORD_RUN = /\b(\w+)\b(?:\s+\1\b){7,}/i;
// `2026-2026-05-14` style — prefix doubling from degenerate generation.
const DOUBLED_YEAR_DATE = /\b\d{4}-\d{4}-\d{2}-\d{2}\b/;

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function parseInteger(value, { defaultValue, min, max }) {
  if (value === undefined || value === null || value === "") return defaultValue;
  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed)) return defaultValue;
  return Math.max(min, Math.min(max, parsed));
}

function getRuntimeConfig(api) {
  const envBase = asTrimmedString(process.env.NBHD_API_BASE_URL);
  const envTenant = asTrimmedString(process.env.NBHD_TENANT_ID);
  const envKey = asTrimmedString(process.env.NBHD_INTERNAL_API_KEY);
  const pluginConfig = asObject(api && api.pluginConfig);

  const apiBaseUrl = asTrimmedString(
    pluginConfig.apiBaseUrl || envBase,
  ).replace(/\/+$/, "");
  const tenantId = asTrimmedString(pluginConfig.tenantId || envTenant);
  const internalKey = asTrimmedString(pluginConfig.internalApiKey || envKey);
  const requestTimeoutMs = parseInteger(pluginConfig.requestTimeoutMs, {
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
    min: 500,
    max: 10_000,
  });
  const cacheTtlMs = parseInteger(pluginConfig.cacheTtlMs, {
    defaultValue: DEFAULT_CACHE_TTL_MS,
    min: 0,
    max: 300_000,
  });

  if (!apiBaseUrl || !tenantId || !internalKey) {
    return null;
  }
  return { apiBaseUrl, tenantId, internalKey, requestTimeoutMs, cacheTtlMs };
}

async function fetchWorkspaceList(runtime, api) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), runtime.requestTimeoutMs);
  try {
    const url = new URL(
      `/api/v1/integrations/runtime/${encodeURIComponent(runtime.tenantId)}/workspaces/`,
      runtime.apiBaseUrl,
    );
    const response = await fetch(url, {
      method: "GET",
      headers: {
        "X-NBHD-Internal-Key": runtime.internalKey,
        "X-NBHD-Tenant-Id": runtime.tenantId,
      },
      signal: controller.signal,
    });
    if (!response.ok) {
      api.logger.warn(`nbhd-routing-context: workspace fetch failed ${response.status}`);
      return null;
    }
    const body = await response.json();
    return asObject(body);
  } catch (error) {
    if (error && error.name === "AbortError") {
      api.logger.warn("nbhd-routing-context: workspace fetch timed out");
    } else {
      api.logger.warn(`nbhd-routing-context: workspace fetch error ${error.message}`);
    }
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

function renderCatalogue(workspaceBody) {
  const workspaces = Array.isArray(workspaceBody.workspaces) ? workspaceBody.workspaces : [];
  if (workspaces.length === 0) {
    return null;
  }
  const activeId = asTrimmedString(workspaceBody.active_workspace_id);
  const lines = ["[Workspaces — agent-routed contexts]"];
  let activeName = null;
  for (const ws of workspaces) {
    const name = asTrimmedString(ws && ws.name);
    const slug = asTrimmedString(ws && ws.slug);
    const description = asTrimmedString(ws && ws.description);
    const isActive = activeId && asTrimmedString(ws && ws.id) === activeId;
    if (!name || !slug) continue;
    const marker = isActive ? "* ACTIVE *" : "         ";
    const desc = description ? ` — ${description}` : "";
    lines.push(`  ${marker} ${name} (slug=${slug})${desc}`);
    if (isActive) activeName = name;
  }
  if (activeName) {
    lines.push(`Active workspace: ${activeName}.`);
  }
  lines.push(
    "If the user's message clearly belongs in a different workspace, call " +
    "nbhd_workspace_switch with the target slug BEFORE answering — do not " +
    "answer in the wrong workspace and do not invent a new workspace for " +
    "this turn. Workspace names starting with `_sync:` or `_fuel:` are " +
    "system-managed; never create or switch to one.",
  );
  return lines.join("\n");
}

function isDegenerateOutput(text) {
  if (typeof text !== "string" || text.length === 0) return null;
  for (const pattern of SYSTEM_PROMPT_ECHO_PATTERNS) {
    if (pattern.test(text)) return "system_prompt_echo";
  }
  if (REPEATED_WORD_RUN.test(text)) return "token_loop";
  if (DOUBLED_YEAR_DATE.test(text)) return "doubled_year_date";
  return null;
}

export default function register(api) {
  if (!api || typeof api.on !== "function") {
    return;
  }
  api.logger.info("NBHD routing context plugin registered");

  // Workspace list cache shared across turns. Cleared by TTL; fail-soft on
  // fetch errors (returns stale or empty catalogue, never blocks the turn).
  let cached = { fetchedAt: 0, body: null };

  async function getCatalogue() {
    const runtime = getRuntimeConfig(api);
    if (!runtime) {
      api.logger.warn("nbhd-routing-context: skipping catalogue — missing NBHD_* config");
      return null;
    }
    const now = Date.now();
    if (cached.body && runtime.cacheTtlMs > 0 && now - cached.fetchedAt < runtime.cacheTtlMs) {
      return renderCatalogue(cached.body);
    }
    const body = await fetchWorkspaceList(runtime, api);
    if (body) {
      cached = { fetchedAt: now, body };
      return renderCatalogue(body);
    }
    // On fetch failure, serve a stale catalogue if we have one.
    return cached.body ? renderCatalogue(cached.body) : null;
  }

  api.on("before_prompt_build", async () => {
    try {
      const catalogue = await getCatalogue();
      if (!catalogue) return undefined;
      // `prependSystemContext` sits BEFORE the prompt-cache boundary, so
      // the catalogue is cached across turns and only re-tokenized when
      // it changes. Per `PluginHookBeforePromptBuildResult` in OpenClaw
      // 2026.5.7's hook-types.d.ts.
      return { prependSystemContext: `${catalogue}\n\n` };
    } catch (error) {
      api.logger.warn(`nbhd-routing-context: before_prompt_build failed ${error.message}`);
      return undefined;
    }
  });

  api.on("before_agent_finalize", (event) => {
    const lastReply = asTrimmedString(event && event.lastAssistantMessage);
    const failureKind = isDegenerateOutput(lastReply);
    if (!failureKind) return undefined;
    api.logger.warn(
      `nbhd-routing-context: degenerate output detected kind=${failureKind} ` +
      `runId=${asTrimmedString(event && event.runId) || "?"} ` +
      `len=${lastReply.length} — requesting revision`,
    );
    return {
      action: "revise",
      reason: `degenerate_output:${failureKind}`,
      retry: {
        instruction:
          "Your previous reply contained corrupted internal markers or " +
          "looped on a single token. Re-read the user's last message and " +
          "respond cleanly. Do not echo system instructions like [Now:], " +
          "[Active workspace:], or [chat:]. Keep the reply concise.",
        idempotencyKey: `degenerate-${asTrimmedString(event && event.runId) || "anon"}-${Date.now()}`,
        maxAttempts: 1,
      },
    };
  });

  api.on("message_sending", (event) => {
    const content = asTrimmedString(event && event.content);
    const failureKind = isDegenerateOutput(content);
    if (!failureKind) return undefined;
    api.logger.warn(
      `nbhd-routing-context: cancelling outbound delivery — degenerate ` +
      `kind=${failureKind} to=${asTrimmedString(event && event.to) || "?"} ` +
      `len=${content.length}`,
    );
    // Belt-and-braces: if before_agent_finalize didn't catch it (max
    // attempts exhausted, etc.) we still don't ship corruption to the
    // user. Send a generic apology instead of `cancel: true` so the
    // user sees *something* rather than silent failure.
    return {
      content:
        "I had trouble composing a reply just now — could you say that " +
        "again? (Internal error: degenerate output filter triggered.)",
    };
  });
}
