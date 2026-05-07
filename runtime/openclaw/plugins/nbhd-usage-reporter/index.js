/**
 * NBHD Usage Reporter Plugin
 *
 * Reports LLM usage from agent turns back to the NBHD control-plane so
 * token/message counters stay synchronized in polling mode.
 *
 * Also listens on `agent_end` and reports BYO provider failures
 * (billing / auth) to the control-plane so the AI Provider page can
 * flip the credential into the `error` state with a user-facing
 * banner. Without this, OpenClaw with `fallbacks: []` raises the
 * billing error to the channel router but the dashboard still shows
 * the BYO route as "Active" — the user has no way to know the route
 * is actually broken.
 */

// Patterns lifted from OpenClaw's `sanitize-user-facing-text` module —
// the runtime classifies billing/auth errors using these same regexes
// before deciding to skip a candidate, so matching them here keeps the
// two reporters aligned. Kept inline (not imported) because the plugin
// SDK does not expose them.
const BILLING_PATTERNS = [
  /["']?(?:status|code)["']?\s*[:=]\s*402\b|\bhttp\s*402\b|\berror(?:\s+code)?\s*[:=]?\s*402\b|^\s*402\s+payment/i,
  /payment required/i,
  /insufficient credits/i,
  /insufficient[_ ]quota/i,
  /credit balance/i,
  /insufficient balance/i,
  /requires?\s+more\s+credits/i,
  /out of extra usage/i,
  /draw from your extra usage/i,
  /extra usage is required/i,
];
const AUTH_PERMANENT_PATTERNS = [
  /invalid api key/i,
  /api key (?:not|is) (?:valid|active|recognized)/i,
  /api key (?:has been )?(?:revoked|disabled|deleted)/i,
  /your account has been (?:suspended|terminated|disabled)/i,
];
const AUTH_PATTERNS = [
  /\b401(?:\s+unauthorized)?\b/i,
  /\b403(?:\s+forbidden)?\b/i,
  /authentication (?:failed|error)/i,
  /unauthorized/i,
  /not authorized/i,
  /token (?:expired|invalid|revoked)/i,
  /session (?:expired|invalid)/i,
  /credentials (?:expired|invalid)/i,
];

function classifyProviderError(message) {
  if (!message || typeof message !== "string") return null;
  if (BILLING_PATTERNS.some((re) => re.test(message))) return "billing";
  if (AUTH_PERMANENT_PATTERNS.some((re) => re.test(message))) return "auth_permanent";
  if (AUTH_PATTERNS.some((re) => re.test(message))) return "auth";
  return null;
}

// BYO routes we know about. Currently only Anthropic CLI subscriptions —
// extend when OpenAI Codex BYO ships.
function detectBYOProvider(modelOrProvider) {
  if (!modelOrProvider) return null;
  const ref = String(modelOrProvider).toLowerCase();
  if (ref.startsWith("anthropic/") || ref === "anthropic") return "anthropic";
  return null;
}

const DEFAULT_REQUEST_TIMEOUT_MS = 5000;

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function asNonNegativeInteger(value) {
  if (value === undefined || value === null || value === "") {
    return null;
  }

  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed) || parsed < 0) {
    return null;
  }

  return parsed;
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
    min: 1000,
    max: 30000,
  });

  if (!apiBaseUrl) {
    throw new Error("NBHD_API_BASE_URL is required");
  }
  if (!tenantId) {
    throw new Error("NBHD_TENANT_ID is required");
  }
  if (!internalKey) {
    throw new Error("NBHD_INTERNAL_API_KEY is required");
  }

  return { apiBaseUrl, tenantId, internalKey, requestTimeoutMs };
}

function extractUsage(event = {}, logger = null) {
  const usage = asObject(event.usage);
  const inputTokens = asNonNegativeInteger(
    usage.input_tokens ?? usage.input,
  );
  const outputTokens = asNonNegativeInteger(
    usage.output_tokens ?? usage.output,
  );
  const modelUsed = asTrimmedString(event.model || event.model_used || "");

  if (inputTokens === null || outputTokens === null) {
    if (logger) {
      logger.warn(
        `NBHD usage extract failed: input_tokens=${inputTokens}, output_tokens=${outputTokens}, ` +
        `model=${modelUsed || "(empty)"}, event_keys=${Object.keys(event)}, usage_keys=${Object.keys(usage)}`,
      );
    }
    return null;
  }

  if (!modelUsed && logger) {
    logger.warn("NBHD usage extract: model is missing, using 'unknown' fallback");
  }

  return {
    event_type: "message",
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    model_used: modelUsed || "unknown",
  };
}

async function postRuntime(path, payload, api, label) {
  let runtime;
  try {
    runtime = getRuntimeConfig(api);
  } catch (error) {
    api.logger.warn(`Skipping ${label} — missing config: ${error.message}`);
    return;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), runtime.requestTimeoutMs);

  try {
    const url = new URL(
      `/api/v1/internal/runtime/${encodeURIComponent(runtime.tenantId)}${path}`,
      runtime.apiBaseUrl,
    );

    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-NBHD-Internal-Key": runtime.internalKey,
        "X-NBHD-Tenant-Id": runtime.tenantId,
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`NBHD ${label} failed (${response.status}): ${text}`);
    }
  } catch (error) {
    if (error && error.name === "AbortError") {
      api.logger.error(`NBHD ${label} timed out`);
      return;
    }
    api.logger.error(`NBHD ${label} failed: ${error.message}`);
  } finally {
    clearTimeout(timeout);
  }
}

async function reportUsage(payload, api) {
  return postRuntime("/usage/report/", payload, api, "usage report");
}

async function reportBYOError(payload, api) {
  return postRuntime("/byo/error/", payload, api, "BYO error report");
}

export default function register(api) {
  if (!api || typeof api.on !== "function") {
    return;
  }

  api.logger.info("NBHD usage reporter plugin registered");

  // Track the most recent provider/model attempted on this agent run.
  // OpenClaw's `agent_end` event payload only carries `success`, `error`,
  // and `messages` — it does not include the provider/model. So we sniff
  // `model_call_started` / `model_call_ended` on the way through and
  // remember the last one for the agent_end branch.
  let lastAttempted = { provider: "", model: "" };

  api.on("model_call_started", (event) => {
    if (event && typeof event === "object") {
      lastAttempted = {
        provider: asTrimmedString(event.provider),
        model: asTrimmedString(event.model),
      };
    }
  });

  api.on("llm_output", (event) => {
    const payload = extractUsage(event, api.logger);
    if (!payload) {
      return;
    }

    void reportUsage(payload, api);
  });

  api.on("agent_end", (event) => {
    if (!event || event.success !== false) return;
    const errorMessage = asTrimmedString(event.error);
    if (!errorMessage) return;

    const reason = classifyProviderError(errorMessage);
    if (!reason) return;

    // Use the provider tag from the last attempted model — `agent_end`
    // doesn't carry it directly. If we didn't see a model_call event
    // (e.g. failure happened before the call started), bail; we don't
    // want to flip the BYO state on errors that aren't tied to a BYO
    // route.
    const provider = detectBYOProvider(lastAttempted.provider) ||
      detectBYOProvider(lastAttempted.model);
    if (!provider) return;

    void reportBYOError(
      {
        provider,
        reason,
        message: errorMessage.slice(0, 500),
        model_used: lastAttempted.model || lastAttempted.provider || "",
      },
      api,
    );
  });
}
